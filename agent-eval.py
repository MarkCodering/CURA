import dotenv

dotenv.load_dotenv()

import argparse
import toml
import resource
import docker
from pathlib import Path
import json
import os
import shutil
import logging
from typing import Optional
from swebench.harness.run_evaluation import run_instances
from swebench.harness.docker_utils import list_images, clean_images
from swebench.harness.docker_build import build_env_images
from langsmith.evaluation import evaluate_existing
from langsmith.schemas import Example, Run
from langsmith.evaluation import evaluate
from langsmith import Client
from cura.prediction import do_prediction_plan
from cura.utils import timeout as timeout_decorator


def main(config):
    logger = logging.getLogger("Evaluation")
    client = Client()

    def predict(inputs: dict):
        version = inputs.get("version", "")
        if isinstance(version, float):
            version = str(version)
        elif not isinstance(version, str):
            logger.warning(f"Unexpected type for version: {type(version)}")
            version = str(version)

        if ":" in version:
            inputs["version"] = version.split(":")[1]
        else:
            logger.warning(f"Version string does not contain ':': {version}")
            inputs["version"] = version

        def get_patch_with_timeout(inputs: dict):
            patch = do_prediction_plan(inputs, config["prediction"])
            return patch

        try:
            patch = get_patch_with_timeout(inputs)
        except Exception as e:
            logger.warning(
                f"Failed to get patch for {inputs['instance_id']} because of {e}"
            )
            patch = ""
        return {
            "instance_id": inputs["instance_id"],
            "model_patch": patch,
            "model_name_or_path": "gpt-4o-mini",
        }

    if config["dataset"]["experiment_name"] == "None":
        limit: Optional[int] = (
            config["dataset"]["count"]
            if isinstance(config["dataset"]["count"], int)
            else None
        )
        eval_result = evaluate(
            predict,
            data=client.list_examples(dataset_id=config["dataset"]["id"], limit=limit),
            max_concurrency=1,
            experiment_prefix="CURA",
        )
        experiment_name = eval_result.experiment_name
    else:
        eval_result = evaluate_existing(config["dataset"]["experiment_name"])
        experiment_name = eval_result.experiment_name
        eval_result = [res for res in eval_result if res["run"].outputs is not None]

    predictions = {
        res["run"].outputs["instance_id"]: {
            **res["run"].outputs,
            "run_id": str(res["run"].id),
        }
        for res in eval_result
    }
    instances = [res["run"].inputs["inputs"] for res in eval_result]
    for instance in instances:
        if "version:" in instance["version"]:
            instance["version"] = instance["version"].split(":")[1]

    RUN_EVALUATION_LOG_DIR = Path("logs/run_evaluation")
    LANGSMITH_EVALUATION_DIR = "./langsmith_feedback/feedback.json"
    RUN_EVALUATION_DIR = "./logs/run_evaluation"

    if os.path.exists(RUN_EVALUATION_DIR):
        shutil.rmtree(RUN_EVALUATION_DIR)

    def convert_runs_to_langsmith_feedback(
        predictions: dict, full_dataset: list, run_id: str
    ) -> float:
        feedback_for_all_instances = {}

        for instance in full_dataset:
            feedback_for_instance = []
            instance_id = instance["instance_id"]
            prediction = predictions[instance_id]
            if prediction.get("model_patch", None) in ["", None]:
                feedback_for_all_instances[prediction["run_id"]] = [
                    {"key": "non-empty-patch", "score": 0},
                    {"key": "completed-patch", "score": 0},
                    {"key": "resolved-patch", "score": 0},
                ]
                continue
            feedback_for_instance.append({"key": "non-empty-patch", "score": 1})
            report_file = (
                RUN_EVALUATION_LOG_DIR
                / run_id
                / prediction["model_name_or_path"].replace("/", "__")
                / prediction["instance_id"]
                / "report.json"
            )
            if report_file.exists():
                feedback_for_instance.append({"key": "completed-patch", "score": 1})
                report = json.loads(report_file.read_text())
                if report[instance_id]["resolved"]:
                    feedback_for_instance.append({"key": "resolved-patch", "score": 1})
                else:
                    feedback_for_instance.append({"key": "resolved-patch", "score": 0})
            else:
                feedback_for_instance += [
                    {"key": "completed-patch", "score": 0},
                    {"key": "resolved-patch", "score": 0},
                ]
            feedback_for_all_instances[prediction["run_id"]] = feedback_for_instance

        os.makedirs(os.path.dirname(LANGSMITH_EVALUATION_DIR), exist_ok=True)
        with open(LANGSMITH_EVALUATION_DIR, "w") as json_file:
            json.dump(feedback_for_all_instances, json_file)

    def evaluate_predictions(
        dataset: list,
        predictions: dict,
        max_workers: int,
        force_rebuild: bool,
        cache_level: str,
        clean: bool,
        open_file_limit: int,
        run_id: str,
        timeout: int,
    ):
        if not dataset:
            raise ValueError(
                "Dataset is empty. Please provide a valid dataset for evaluation."
            )

        assert len(run_id) > 0, "Run ID must be provided"
        resource.setrlimit(resource.RLIMIT_NOFILE, (open_file_limit, open_file_limit))
        client = docker.from_env()

        existing_images = list_images(client)
        print(f"Running {len(dataset)} unevaluated instances...")

        build_env_images(client, dataset, force_rebuild, max_workers)
        run_instances(
            predictions,
            dataset,
            cache_level,
            clean,
            force_rebuild,
            max_workers,
            run_id,
            timeout,
        )

        clean_images(client, existing_images, cache_level, clean)

        convert_runs_to_langsmith_feedback(predictions, dataset, run_id)

    evaluate_predictions(
        instances,
        predictions,
        max_workers=8,
        force_rebuild=False,
        cache_level="env",
        clean=False,
        open_file_limit=4096,
        run_id="test",
        timeout=1_800,
    )

    def swe_bench_evaluator(run: Run, example: Example):
        with open(LANGSMITH_EVALUATION_DIR, "r") as json_file:
            langsmith_eval = json.load(json_file)
        if str(run.id) in langsmith_eval:
            return {"results": langsmith_eval[str(run.id)]}
        else:
            return {"results": []}

    evaluate_existing(experiment_name, evaluators=[swe_bench_evaluator])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=str, default="configs/default_config.toml")
    args = parser.parse_args()
    config = toml.load(args.config)
    main(config)
