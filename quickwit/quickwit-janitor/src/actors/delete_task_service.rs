// Copyright (C) 2024 Quickwit, Inc.
//
// Quickwit is offered under the AGPL v3.0 and as commercial software.
// For commercial licensing, contact us at hello@quickwit.io.
//
// AGPL:
// This program is free software: you can redistribute it and/or modify
// it under the terms of the GNU Affero General Public License as
// published by the Free Software Foundation, either version 3 of the
// License, or (at your option) any later version.
//
// This program is distributed in the hope that it will be useful,
// but WITHOUT ANY WARRANTY; without even the implied warranty of
// MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
// GNU Affero General Public License for more details.
//
// You should have received a copy of the GNU Affero General Public License
// along with this program. If not, see <http://www.gnu.org/licenses/>.

use std::collections::{HashMap, HashSet};
use std::path::PathBuf;
use std::time::Duration;

use async_trait::async_trait;
use quickwit_actors::{Actor, ActorContext, ActorExitStatus, ActorHandle, Handler};
use quickwit_common::pubsub::EventBroker;
use quickwit_common::temp_dir::{self};
use quickwit_config::IndexConfig;
use quickwit_metastore::{IndexMetadataResponseExt, ListIndexesMetadataResponseExt};
use quickwit_proto::metastore::{
    IndexMetadataRequest, ListIndexesMetadataRequest, MetastoreService, MetastoreServiceClient,
};
use quickwit_proto::types::IndexUid;
use quickwit_search::SearchJobPlacer;
use quickwit_storage::StorageResolver;
use serde::Serialize;
use tracing::{error, info, warn};

use super::delete_task_pipeline::DeleteTaskPipeline;

pub const DELETE_SERVICE_TASK_DIR_NAME: &str = "delete_task_service";

const UPDATE_PIPELINES_INTERVAL: Duration = if cfg!(any(test, feature = "testsuite")) {
    Duration::from_millis(200)
} else {
    // Each update triggers a call to the metastore. Deletes are not frequent operation and
    // it's fine to wait a bit before updating the pipelines.
    Duration::from_secs(30)
};

#[derive(Debug, Clone, Serialize)]
pub struct DeleteTaskServiceState {
    pub num_running_pipelines: usize,
}

pub struct DeleteTaskService {
    metastore: MetastoreServiceClient,
    search_job_placer: SearchJobPlacer,
    storage_resolver: StorageResolver,
    delete_service_task_dir: PathBuf,
    pipeline_handles_by_index_uid: HashMap<IndexUid, ActorHandle<DeleteTaskPipeline>>,
    max_concurrent_split_uploads: usize,
    event_broker: EventBroker,
}

impl DeleteTaskService {
    pub async fn new(
        metastore: MetastoreServiceClient,
        search_job_placer: SearchJobPlacer,
        storage_resolver: StorageResolver,
        data_dir_path: PathBuf,
        max_concurrent_split_uploads: usize,
        event_broker: EventBroker,
    ) -> anyhow::Result<Self> {
        let delete_service_task_path = data_dir_path.join(DELETE_SERVICE_TASK_DIR_NAME);
        let delete_service_task_dir =
            temp_dir::create_or_purge_directory(delete_service_task_path.as_path()).await?;
        Ok(Self {
            metastore,
            search_job_placer,
            storage_resolver,
            delete_service_task_dir,
            pipeline_handles_by_index_uid: Default::default(),
            max_concurrent_split_uploads,
            event_broker,
        })
    }
}

#[async_trait]
impl Actor for DeleteTaskService {
    type ObservableState = DeleteTaskServiceState;

    fn observable_state(&self) -> Self::ObservableState {
        DeleteTaskServiceState {
            num_running_pipelines: self.pipeline_handles_by_index_uid.len(),
        }
    }

    fn name(&self) -> String {
        "DeleteTaskService".to_string()
    }

    async fn initialize(&mut self, ctx: &ActorContext<Self>) -> Result<(), ActorExitStatus> {
        self.handle(UpdatePipelines, ctx).await?;
        Ok(())
    }
}

impl DeleteTaskService {
    pub async fn update_pipeline_handles(
        &mut self,
        ctx: &ActorContext<Self>,
    ) -> anyhow::Result<()> {
        let mut index_config_by_index_id: HashMap<IndexUid, IndexConfig> = self
            .metastore
            .list_indexes_metadata(ListIndexesMetadataRequest::all())
            .await?
            .deserialize_indexes_metadata()?
            .into_iter()
            .map(|index_metadata| {
                (
                    index_metadata.index_uid.clone(),
                    index_metadata.into_index_config(),
                )
            })
            .collect();
        let index_uids: HashSet<IndexUid> = index_config_by_index_id.keys().cloned().collect();
        let pipeline_index_uids: HashSet<IndexUid> =
            self.pipeline_handles_by_index_uid.keys().cloned().collect();

        // Remove pipelines on deleted indexes.
        for deleted_index_uid in pipeline_index_uids.difference(&index_uids) {
            info!(
                deleted_index_id = deleted_index_uid.index_id(),
                "Remove deleted index from delete task pipelines."
            );
            let pipeline_handle = self
                .pipeline_handles_by_index_uid
                .remove(deleted_index_uid)
                .expect("Handle must be present.");
            // Kill the pipeline, this avoids to wait a long time for a delete operation to finish.
            pipeline_handle.kill().await;
        }

        // Start new pipelines and add them to the handles hashmap.
        for index_uid in index_uids.difference(&pipeline_index_uids) {
            let index_config = index_config_by_index_id
                .remove(index_uid)
                .expect("Index metadata must be present.");
            if self.spawn_pipeline(index_config, ctx).await.is_err() {
                warn!(
                    "Failed to spawn delete pipeline for {}",
                    index_uid.index_id()
                );
            }
        }

        Ok(())
    }

    pub async fn spawn_pipeline(
        &mut self,
        index_config: IndexConfig,
        ctx: &ActorContext<Self>,
    ) -> anyhow::Result<()> {
        let index_uri = index_config.index_uri.clone();
        let index_storage = self.storage_resolver.resolve(&index_uri).await?;
        let index_metadata_request =
            IndexMetadataRequest::for_index_id(index_config.index_id.to_string());
        let index_metadata = self
            .metastore
            .index_metadata(index_metadata_request)
            .await?
            .deserialize_index_metadata()?;
        let pipeline = DeleteTaskPipeline::new(
            index_metadata.index_uid.clone(),
            self.metastore.clone(),
            self.search_job_placer.clone(),
            index_storage,
            self.delete_service_task_dir.clone(),
            self.max_concurrent_split_uploads,
            self.event_broker.clone(),
        );
        let (_pipeline_mailbox, pipeline_handler) = ctx.spawn_actor().spawn(pipeline);
        self.pipeline_handles_by_index_uid
            .insert(index_metadata.index_uid, pipeline_handler);
        Ok(())
    }
}

#[derive(Debug)]
struct UpdatePipelines;

#[async_trait]
impl Handler<UpdatePipelines> for DeleteTaskService {
    type Reply = ();

    async fn handle(
        &mut self,
        _: UpdatePipelines,
        ctx: &ActorContext<Self>,
    ) -> Result<(), ActorExitStatus> {
        let result = self.update_pipeline_handles(ctx).await;
        if let Err(error) = result {
            error!(error=%error, "delete task pipelines update failed");
        }
        ctx.schedule_self_msg(UPDATE_PIPELINES_INTERVAL, UpdatePipelines);
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use quickwit_common::pubsub::EventBroker;
    use quickwit_indexing::TestSandbox;
    use quickwit_proto::metastore::{
        DeleteIndexRequest, DeleteQuery, ListDeleteTasksRequest, MetastoreService,
    };
    use quickwit_search::{searcher_pool_for_test, MockSearchService, SearchJobPlacer};
    use quickwit_storage::StorageResolver;

    use super::{DeleteTaskService, UPDATE_PIPELINES_INTERVAL};

    #[tokio::test]
    async fn test_delete_task_service() -> anyhow::Result<()> {
        quickwit_common::setup_logging_for_tests();
        let index_id = "test-delete-task-service-index";
        let doc_mapping_yaml = r#"
            field_mappings:
              - name: body
                type: text
              - name: ts
                type: i64
                fast: true
        "#;
        let test_sandbox = TestSandbox::create(index_id, doc_mapping_yaml, "{}", &["body"]).await?;
        let index_uid = test_sandbox.index_uid();
        let mut metastore = test_sandbox.metastore();
        let mock_search_service = MockSearchService::new();
        let searcher_pool = searcher_pool_for_test([("127.0.0.1:1000", mock_search_service)]);
        let search_job_placer = SearchJobPlacer::new(searcher_pool);
        let temp_dir = tempfile::tempdir().unwrap();
        let data_dir_path = temp_dir.path().to_path_buf();
        let delete_task_service = DeleteTaskService::new(
            metastore.clone(),
            search_job_placer,
            StorageResolver::unconfigured(),
            data_dir_path,
            4,
            EventBroker::default(),
        )
        .await
        .unwrap();
        let (_delete_task_service_mailbox, delete_task_service_handler) = test_sandbox
            .universe()
            .spawn_builder()
            .spawn(delete_task_service);
        let state = delete_task_service_handler
            .process_pending_and_observe()
            .await;
        assert_eq!(state.num_running_pipelines, 1);
        let delete_query = DeleteQuery {
            index_uid: index_uid.to_string(),
            start_timestamp: None,
            end_timestamp: None,
            query_ast: r#"{"type": "MatchAll"}"#.to_string(),
        };
        metastore.create_delete_task(delete_query).await.unwrap();
        // Just test creation of delete query.
        assert_eq!(
            metastore
                .list_delete_tasks(ListDeleteTasksRequest::new(index_uid.clone(), 0))
                .await
                .unwrap()
                .delete_tasks
                .len(),
            1
        );
        metastore
            .delete_index(DeleteIndexRequest {
                index_uid: index_uid.to_string(),
            })
            .await
            .unwrap();
        test_sandbox
            .universe()
            .sleep(UPDATE_PIPELINES_INTERVAL * 2)
            .await;
        let state_after_deletion = delete_task_service_handler
            .process_pending_and_observe()
            .await;
        assert_eq!(state_after_deletion.num_running_pipelines, 0);
        assert!(test_sandbox
            .universe()
            .get_one::<DeleteTaskService>()
            .is_some());
        let actors_observations = test_sandbox
            .universe()
            .observe(UPDATE_PIPELINES_INTERVAL)
            .await;
        assert!(
            actors_observations
                .into_iter()
                .any(|observation| observation.type_name
                    == std::any::type_name::<DeleteTaskService>())
        );
        assert!(test_sandbox
            .universe()
            .get_one::<DeleteTaskService>()
            .is_some());
        test_sandbox.assert_quit().await;
        Ok(())
    }
}
