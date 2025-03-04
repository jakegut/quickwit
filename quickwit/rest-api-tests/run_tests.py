#!/usr/bin/env python3

import subprocess
import requests
import glob
import yaml
import sys
from os import path as osp
from os import mkdir
import gzip
import http
import json
import tempfile
import shutil
import time

def debug_http():
    old_send = http.client.HTTPConnection.send
    def new_send(self, data):
        print(f'{"-"*9} BEGIN REQUEST {"-"*9}')
        if len(data) > 500:
            print("Data too big")
            print(data[:500])
        else:
            print(data.decode('utf-8').strip())
        print(f'{"-"*10} END REQUEST {"-"*10}')
        return old_send(self, data)
    http.client.HTTPConnection.send = new_send

# debug_http()

def open_scenario(scenario_filepath):
    data = open(scenario_filepath).read()
    steps_data = data.split("\n---")
    for step_data in steps_data:
        step_data  = step_data.strip()
        if step_data == "":
            continue
        step_dict = yaml.load(step_data, Loader=yaml.Loader)
        if type(step_dict) == dict:
            yield step_dict

def run_step(step, previous_result):
    result = {}
    if "method" in step:
        methods = step["method"]
        if type(methods) != list:
            methods = [methods]
        for method in methods:
            result = run_request_step(method, step, previous_result)
    return result

def load_data(path):
    if path.endswith("gz"):
        return gzip.open(path, 'rb').read()
    else:
        return open(path, 'rb').read()

def run_request_with_retry(run_req, expected_status_code=None, num_retries=10, wait_time=0.5):
    for try_number in range(num_retries + 1):
        r = run_req()
        if expected_status_code is None or r.status_code == expected_status_code:
            return r
        print("Failed with", r.text, r.status_code)
        if try_number < num_retries:
            print("Retrying...")
            time.sleep(wait_time)
    raise Exception("Wrong status code. Got %s, expected %s, url %s" % (r.status_code, expected_status_code, run_req().url))


def resolve_previous_result(c, previous_result):
    if type(c) == dict:
        result = {}
        if len(c) == 1 and "$previous" in c:
            return eval(c["$previous"], None, {"val": previous_result})
        for (k, v) in c.items():
            result[k] = resolve_previous_result(v, previous_result)
        return result
    if type(c) == list:
        return [
            resolve_previous_result(v, previous_result)
            for v in c
        ]
    return c

def run_request_step(method, step, previous_result):
    assert method in {"GET", "POST", "PUT", "DELETE"}
    if "headers" not in step:
        step["headers"] = {'user-agent': 'my-app/0.0.1'}
    method_req = getattr(requests, method.lower())
    endpoint = step.get("endpoint", "")
    url = step["api_root"] + endpoint
    kvargs = {
        k: v
        for k, v in step.items()
        if k in {"params", "data", "json", "headers"}
    }
    body_from_file = step.get("body_from_file", None)
    if body_from_file is not None:
        body_from_file = osp.join(step["cwd"], body_from_file)
        kvargs["data"] = load_data(body_from_file)
    kvargs = resolve_previous_result(kvargs, previous_result)
    ndjson = step.get("ndjson", None)
    if ndjson is not None:
        # Add a newline at the end to please elasticsearch -> "The bulk request must be terminated by a newline [\\n]".
        kvargs["data"] = "\n".join([json.dumps(doc) for doc in ndjson]) + "\n"
        kvargs.setdefault("headers")["Content-Type"] = "application/json"
    expected_status_code = step.get("status_code", 200)
    num_retries = step.get("num_retries", 0)
    run_req = lambda : method_req(url, **kvargs)
    r = run_request_with_retry(run_req, expected_status_code, num_retries)
    expected_resp = step.get("expected", None)
    json_res = r.json()
    if expected_resp is not None:
        try:
            check_result(json_res, expected_resp, context_path="")
        except Exception as e:
            print(json.dumps(json_res, indent=2))
            raise e
    return json_res

def check_result(result, expected, context_path = ""):
    if type(expected) == dict and "$expect" in expected:
        expectations = expected["$expect"]
        if type(expectations) == str:
            expectations = [expectations]
        for expectation in expectations:
            if not eval(expectation, None, {"val": result}):
                print(result)
                raise Exception("Failed to meet expectation %s at %s" % (expectation, context_path))
            return
    if type(result) != type(expected):
        raise Exception("Wrong type at context %s. Got %s, Expected %s" % (context_path, type(result), type(expected)))
    elif type(result) == dict:
        check_result_dict(result, expected, context_path)
    elif type(result) == list:
        check_result_list(result, expected, context_path)
    elif result != expected:
        raise Exception("Expected %s at context %s, got %s" % (expected, context_path, result))

def check_result_list(result, expected, context_path=""):
    if len(result) != len(expected):
        raise(Exception("Wrong length at context %s" % context_path))
    for (i, (left, right)) in enumerate(zip(result, expected)):
        check_result(left, right, context_path + "[%s]" % i)

def check_result_dict(result, expected, context_path=""):
    for (k, v) in expected.items():
        child = result.get(k, None)
        if child is None:
            raise Exception("Missing key %s at context %s" % (k, context_path))
        check_result(child, v, context_path + "." + k)

class PathTree:
    def __init__(self):
        self.children = {}
        self.scripts = []

    def add_child(self, seg):
        child = self.children.get(seg, None)
        if child is None:
            self.children[seg] = PathTree()
        return self.children[seg]

    def add_script(self, script):
        self.scripts.append(script)

    def add_path(self, path):
        path_segs = path.split("/")
        if path_segs[-1].startswith("_"):
            return
        path_tree = self
        for path_seg in path_segs[:-1]:
            path_tree = path_tree.add_child(path_seg)
        path_tree.add_script(path_segs[-1])

    def visit_nodes(self, visitor, path=[]):
        success = True
        success &= visitor.enter_directory(path)
        for script in self.scripts:
            success &= visitor.run_scenario(path, script)
        for k in sorted(self.children.keys()):
            child_path = path + [k]
            success &= self.children[k].visit_nodes(visitor, child_path)
        success &= visitor.exit_directory(path)
        return success

# Returns a new dictionary without modifying the arguments.
# The new dictionary is the result of merging the two dictionaries
# in that order:
# The second dictionary may shadow/override the keys of the first dictionar
def stack_dicts(context, overriding):
    context = context.copy()
    context.update(overriding)
    return context

class Visitor:
    def __init__(self, engine):
        self.engine = engine
        self.context_stack = []
        self.context = {}
    def run_setup_teardown_scripts(self, script_name, path):
        cwd = "/".join(path)
        success = True
        for file_name in [script_name + ".yaml", script_name + "." + self.engine + ".yaml"]:
            script_fullpath = cwd + "/" + file_name
            if osp.exists(script_fullpath):
                success &= self.run_scenario(path, file_name)
        return success
    def load_context(self, path):
        context = {"cwd": "/".join(path)}
        for file_name in ["_ctx.yaml", "_ctx." + self.engine + ".yaml"]:
            ctx_filepath = "/".join(path + [file_name])
            if osp.exists(ctx_filepath):
                ctx = yaml.load(open(ctx_filepath), Loader=yaml.Loader)
                context.update(ctx)
        self.context_stack.append(context)
        self.context.update(context)
    def enter_directory(self, path):
        print("============")
        self.load_context(path)
        return self.run_setup_teardown_scripts("_setup", path)
    def exit_directory(self, path):
        success = self.run_setup_teardown_scripts("_teardown", path)
        self.context_stack.pop()
        self.context = {}
        for ctx in self.context_stack:
            self.context.update(ctx)
        return success
    def run_scenario(self, path, script):
        scenario_path = "/".join(path + [script])
        steps = list(open_scenario(scenario_path))
        num_steps_executed = 0
        num_steps_skipped = 0
        previous_result = {}
        for (i, step) in enumerate(steps, 1):
            step = stack_dicts(self.context, step)
            applicable_engine = step.get("engines", None)
            if applicable_engine is not None:
                if self.engine not in applicable_engine:
                    num_steps_skipped += 1
                    continue
            try:
                previous_result = run_step(step, previous_result)
                num_steps_executed += 1
            except Exception as e:
                print("🔴 %s" % scenario_path)
                print("Failed at step %d" % i)
                print(step)
                print(e)
                print("--------------")
                return False
        else:
            print("🟢 %s: %d steps (%d skipped)" % (scenario_path, num_steps_executed, num_steps_skipped))
        return True

def build_path_tree(paths):
    paths.sort()
    path_tree = PathTree()
    for path in paths:
        path_tree.add_path(path)
    return path_tree

def run(scenario_paths, engine):
    path_tree = build_path_tree(scenario_paths)
    visitor = Visitor(engine=engine)
    return path_tree.visit_nodes(visitor)

def filter_test(prefixes, test_name):
    for prefix in prefixes:
        if test_name.startswith(prefix):
            return True
    return False

def filter_tests(prefixes, test_names):
    print("Filtering tests prefixes: %s" % prefixes)
    if prefixes is None or len(prefixes) == 0:
        return test_names
    return [
        test_name
        for test_name in test_names
        if filter_test(prefixes, test_name)
    ]

class QuickwitRunner:
    def __init__(self, quickwit_bin_path):
        self.quickwit_dir = tempfile.TemporaryDirectory()
        print('created temporary directory', self.quickwit_dir, self.quickwit_dir.name)
        qwdata = osp.join(self.quickwit_dir.name, "qwdata")
        config = osp.join(self.quickwit_dir.name, "config")
        mkdir(qwdata)
        mkdir(config)
        shutil.copy("../../config/quickwit.yaml", config)
        shutil.copy(quickwit_bin_path, self.quickwit_dir.name)
        self.proc = subprocess.Popen(["./quickwit", "run"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=self.quickwit_dir.name)
        for i in range(100):
            try:
                print("Checking on quickwit")
                res = requests.get("http://localhost:7280/health/readyz")
                if res.status_code == 200 and res.text.strip() == "true":
                    print("Quickwit started")
                    time.sleep(6)
                    break
            except:
                pass
            print("Server not ready yet. Sleep and retry...")
            time.sleep(1)
        else:
            print("Quickwit never started. Exiting.")
            sys.exit(2)
    def __del__(self):
        print("Killing Quickwit")
        subprocess.Popen.kill(self.proc)

def main():
    import argparse
    arg_parser = argparse.ArgumentParser(
        prog="rest-api-test",
        description="Runs a set of calls against a REST API and checks for conditions over the results."
    )
    arg_parser.add_argument("--engine", help="Targetted engine (elastic/quickwit).", default="quickwit")
    arg_parser.add_argument("--test", help="Specific prefix to select the tests to run. If not specified, all tests are run.", nargs="*")
    arg_parser.add_argument("--binary", help="Specific the quickwit binary to run.", nargs="?")
    parsed_args = arg_parser.parse_args()

    print(parsed_args)

    quickwit_process = None
    if parsed_args.binary is not None:
        if parsed_args.engine != "quickwit":
            print("The --binary option is only supported for quickwit engine.")
            sys.exit(3)
        binary = parsed_args.binary
        quickwit_process = QuickwitRunner(binary)
    quickwit_process

    scenario_filepaths = glob.glob("scenarii/**/*.yaml", recursive=True)
    scenario_filepaths = list(filter_tests(parsed_args.test, scenario_filepaths))
    return run(scenario_filepaths, engine=parsed_args.engine)

if __name__ == "__main__":
    import sys
    if main():
        sys.exit(0)
    else:
        sys.exit(1)

