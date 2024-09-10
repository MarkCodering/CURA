
import dotenv
dotenv.load_dotenv()
from swebench.harness.run_evaluation import run_instances
import resource
import docker
from swebench.harness.docker_utils import list_images, clean_images
from swebench.harness.docker_build import build_env_images
from pathlib import Path
import json
import os
from langsmith.evaluation import evaluate_existing
from langsmith.schemas import Example, Run


from langsmith.evaluation import evaluate
from langsmith import Client
from cura.prediction import do_prediction_plan
import random
from swebench.harness.constants import USE_X86
import platform
import shutil
from cura.utils import timeout



client = Client()
def predict(inputs: dict):
    @timeout(500)
    def get_patch_with_timeout(inputs: dict):
        patch = do_prediction_plan(inputs)
        return patch
    try:
        patch = get_patch_with_timeout(inputs)
    except Exception:
        patch = ""
    return {
        "instance_id":inputs['instance_id'],
        "model_patch": patch,
        "model_name_or_path":"gpt-4o-mini"
    }

dataset = list(client.list_examples(dataset_id="b0d01694-cdf6-4142-a93f-29b891d23682"))
print(len(dataset))
"""
for d in dataset:
    d.inputs['version'] = d.inputs['version'].split(":")[1]

if platform.machine() == 'arm64':
    dataset = [d for d in dataset if d.inputs['instance_id'] not in USE_X86]

#random.seed(42)
#dataset = random.sample(dataset, 20)
"""

eval_result = evaluate(
    predict,
    data=dataset,
    max_concurrency=1,
)
"""
from langsmith.evaluation import evaluate_existing
experiment_name = "bold-music-5" # Replace with an actual experiment name or ID

eval_result = []
eval_result = evaluate_existing(experiment_name)
"""
dataset = []
predictions = {}
for res in eval_result:
    predictions[res['run'].outputs['instance_id']] = {**res['run'].outputs,**{"run_id":str(res['run'].id)}}
    dataset.append(res['run'].inputs['inputs'])
for d in dataset:
    d['version'] = d['version'].split(":")[1]

import os
import shutil
import json
import docker
import resource
from pathlib import Path

RUN_EVALUATION_LOG_DIR = Path("logs/run_evaluation")
LANGSMITH_EVALUATION_DIR = './langsmith_feedback/feedback.json'
RUN_EVALUATION_DIR = './logs/run_evaluation'

# Clean up the existing run evaluation directory if it exists
if os.path.exists(RUN_EVALUATION_DIR):
    shutil.rmtree(RUN_EVALUATION_DIR)


def convert_runs_to_langsmith_feedback(predictions: dict, full_dataset: list, run_id: str) -> float:
    """
    Convert logs from docker containers into LangSmith feedback.

    Args:
        predictions (dict): Predictions dict generated by the model
        full_dataset (list): List of all instances
        run_id (str): Run ID
    """
    feedback_for_all_instances = {}

    for instance in full_dataset:
        feedback_for_instance = []
        instance_id = instance['instance_id']
        prediction = predictions[instance_id]
        if prediction.get("model_patch", None) in ["", None]:
            # Prediction returned an empty patch
            feedback_for_all_instances[prediction['run_id']] = [{"key": "non-empty-patch", "score": 0},
                                                                {"key": "completed-patch", "score": 0},
                                                                {"key": "resolved-patch", "score": 0}]
            continue
        feedback_for_instance.append({"key": "non-empty-patch", "score": 1})
        report_file = (
            RUN_EVALUATION_LOG_DIR
            / run_id
            / prediction["model_name_or_path"].replace("/", "__")
            / prediction['instance_id']
            / "report.json"
        )
        if report_file.exists():
            # If report file exists, then the instance has been run
            feedback_for_instance.append({"key": "completed-patch", "score": 1})
            report = json.loads(report_file.read_text())
            # Check if instance actually resolved the PR
            if report[instance_id]["resolved"]:
                feedback_for_instance.append({"key": "resolved-patch", "score": 1})
            else:
                feedback_for_instance.append({"key": "resolved-patch", "score": 0})
        else:
            # The instance did not run successfully
            feedback_for_instance += [{"key": "completed-patch", "score": 0}, {"key": "resolved-patch", "score": 0}]
        feedback_for_all_instances[prediction['run_id']] = feedback_for_instance

    os.makedirs(os.path.dirname(LANGSMITH_EVALUATION_DIR), exist_ok=True)
    with open(LANGSMITH_EVALUATION_DIR, 'w') as json_file:
        json.dump(feedback_for_all_instances, json_file)


def get_test_specs_from_dataset(dataset: list):
    """
    Fetch test specs from the dataset.
    This function maps the dataset into test specifications.
    """
    if not dataset:
        raise ValueError("Dataset is empty. Cannot get test specifications.")

    return list(map(make_test_spec, dataset))


def make_test_spec(instance):
    """
    Handle the versioning issue and return the correct specs.
    """
    repo = instance.get('repo')
    version = instance.get('version')

    # Example mapping (you should replace this with the real MAP_REPO_VERSION_TO_SPECS)
    MAP_REPO_VERSION_TO_SPECS = {
        'repo1': {
            'version:1.0': {},
            'version:2.0': {},
            'version:3.0': {},  # You might be missing version:3.1 here
            'version:3.1': {},  # Add the specs for version:3.1 here
        }
    }

    # Try to find the correct specs
    if repo not in MAP_REPO_VERSION_TO_SPECS:
        raise KeyError(f"Repository '{repo}' not found in MAP_REPO_VERSION_TO_SPECS.")
    
    # Fallback for missing version: use version:3.0 if version:3.1 is missing
    if version == 'version:3.1' and 'version:3.1' not in MAP_REPO_VERSION_TO_SPECS[repo]:
        version = 'version:3.0'

    if version not in MAP_REPO_VERSION_TO_SPECS[repo]:
        raise KeyError(f"Version '{version}' not found for repo '{repo}'. Available versions: {list(MAP_REPO_VERSION_TO_SPECS[repo].keys())}")

    return MAP_REPO_VERSION_TO_SPECS[repo][version]


def evaluate_predictions(dataset: list, predictions: dict, max_workers: int, force_rebuild: bool,
                         cache_level: str, clean: bool, open_file_limit: int, run_id: str, timeout: int):
    """
    Run evaluation harness for the given dataset and predictions.
    """
    # Check if dataset is empty
    if not dataset:
        raise ValueError("Dataset is empty. Please provide a valid dataset for evaluation.")

    # Set open file limit
    assert len(run_id) > 0, "Run ID must be provided"
    resource.setrlimit(resource.RLIMIT_NOFILE, (open_file_limit, open_file_limit))
    client = docker.from_env()

    # List existing images to clean them up later
    existing_images = list_images(client)
    print(f"Running {len(dataset)} unevaluated instances...")

    # Build environment images + run instances
    build_env_images(client, dataset, force_rebuild, max_workers)
    run_instances(predictions, dataset, cache_level, clean, force_rebuild, max_workers, run_id, timeout)

    # Clean images + make final report
    clean_images(client, existing_images, cache_level, clean)

    # Convert the logs into feedback for LangSmith
    convert_runs_to_langsmith_feedback(predictions, dataset, run_id)

# Example call to evaluate_predictions (replace with actual instances and predictions)
evaluate_predictions(dataset,predictions,max_workers=8,force_rebuild=False,cache_level="env",clean=False \
                     ,open_file_limit=4096,run_id="test",timeout=1_800)


def swe_bench_evaluator(run, example):
    """
    SWE Bench evaluator to return LangSmith evaluation results.
    """
    with open(LANGSMITH_EVALUATION_DIR, 'r') as json_file:
        langsmith_eval = json.load(json_file)
    return {"results": langsmith_eval[str(run.id)]}

evaluate_existing("bold-music-5", swe_bench_evaluator)