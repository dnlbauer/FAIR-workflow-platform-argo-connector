import requests
from requests_toolbelt import MultipartEncoder
import json
import logging
from datetime import datetime
import os
from pathlib import Path
import tempfile
from urllib.parse import urljoin

import magic
import cordra
from typing import Any, Generator
import yaml

logger = logging.getLogger("uvicorn.error")

def check_health(host: str, user: str, password: str) -> bool|str:
    try:
        schemas = cordra.CordraObject.find(host=host, username=user, password=password, verify=False, query="type:Schema")
        if schemas.get("size", 0) == 0:
            return "No schemas found"
    except Exception as e:
        return str(e)

    return True

def create_dataset_from_workflow_artifacts(host: str, user: str, password: str, wfl: dict[str: Any], artifact_stream_iterator: Generator, reconstructed_wfl: dict[str: Any], skip_content: bool = False, file_max_size: int = 100*1024*1024) -> str:
    upload_kwargs = {
        "host": host,
        "username": user,
        "password": password,
        "verify": False,
    }

    workflow_annotations = reconstructed_wfl["metadata"]["annotations"]

    created_ids = {}
    created_files = {}
    try:
        create_action_author = None
        for key, value in workflow_annotations.items():
            if key.startswith("argo-connector/submitterId"):
                number = key.split("argo-connector/submitterId")[1]

                author_id = "https://orcid.org/" + wfl["metadata"]["annotations"][f"argo-connector/submitterId{number}"]
                author_properties = {"identifier": author_id}
                if f"argo-connector/submitterName{number}" in wfl["metadata"]["annotations"]:
                    author_properties["name"] = wfl["metadata"]["annotations"][f"argo-connector/submitterName{number}"]
                author = cordra.CordraObject.create(obj_type="Person", obj_json=author_properties, **upload_kwargs)
                if number == "1":
                    create_action_author = author
                created_ids[author["@id"]] = "Person"

        # upload files
        logger.debug("Creating file objects")
        for file_name, content_iterator in artifact_stream_iterator:

            logger.debug("Creating FileObject from " + file_name)
            relative_path = file_name
            # Write file content into temporary file
            # Streaming the file directly into cordra would be better, but this didnt work for me.
            with tempfile.NamedTemporaryFile(delete=True,
                                             prefix=f"argo-artifact-tmp-{os.path.basename(file_name)}-") as tmp_file:

                if not skip_content:
                    try:
                        logger.debug("Downloading content to temp file: " + tmp_file.name)
                        for chunk in content_iterator:
                            if chunk:
                                tmp_file.write(chunk)
                    except Exception as e:
                        logger.error("Failed to download file content: " + str(e))
                        content_iterator.close()
                        raise e
                    tmp_file.flush()
                    file_size = Path(tmp_file.name).stat().st_size

                    logger.debug(f"Download done ({file_size / 1024 / 1024:.2f} MB)")

                    # Cordra has issues with huge files
                    if file_max_size > 0 and file_size > file_max_size:
                        logger.warning(f"File size is {file_size / 1024 / 1024:.2f} MB, which is too large to upload. Skipping...")
                        continue

                    # figure out file encoding
                    try:
                        encoding_format = magic.from_file(tmp_file.name, mime=True)
                        logger.debug("Infered encoding format: " + encoding_format)
                    except magic.MagicException as e:
                        logger.warning(f"Failed to get encoding format for {file_name}")
                        encoding_format = None
                else:
                    content_iterator.close()
                    encoding_format = None

                # write object
                logger.debug("Sending file to cordra")
                obj_json={
                    "name": os.path.basename(relative_path),
                    "contentSize": Path(tmp_file.name).stat().st_size,
                    "encodingFormat": encoding_format,
                    "contentUrl": relative_path,
                }
                # Cordra library does not stream files, and therefore goes OOM for large payloads.
                # This request should take care of it
                multipart = MultipartEncoder(fields={
                    "content": json.dumps(obj_json),
                    "file": (relative_path, open(tmp_file.name, "rb"), encoding_format)
                })
                response = requests.post(urljoin(host, "objects/") + "?type=FileObject", data=multipart, headers={"Content-Type": multipart.content_type}, verify=False, auth=(user, password))
                response.raise_for_status()
                file_obj = response.json()
                logger.debug("File ingested")
            created_ids[file_obj["@id"]] = "FileObject"
            created_files[file_obj["@id"]] = file_obj["contentUrl"]

        # For ModGP, we want to create one dataset per species.
        # The workflow cannot produce that, so we create them manually based on the folder structure
        # For ModGP Workflows (which is detected based on the word ModGP in file paths),
        # we create one nested dataset per species subfolder in Exports/ModGP,
        # and remove corresponding files from the root dataset.
        is_modGP = len(list(filter(lambda path: "Exports/ModGP" in path, created_files.values()))) > 0
        if is_modGP:
            nested_dataset_properties = {
                "author": [id for id in created_ids if created_ids[id] == "Person"],
                "license": workflow_annotations.get("argo-connector/license", None),
                "keywords": [keyword for keyword in workflow_annotations.get("argo-connector/keywords", "").split(",") if keyword != ""],
            }

            nested_datasets_items = {}
            for id, file_path in [(id, created_files[id]) for id in created_files]:
                splitted_path = file_path.split("/")
                if len(splitted_path) > 6 and splitted_path[2] == "Exports" and splitted_path[3] == "ModGP":
                    genus = splitted_path[4]
                    species = splitted_path[5]
                    dataset_name = f"{genus} {species}"
                    nested_datasets_items[dataset_name] = nested_datasets_items.get(dataset_name, []) + [id]

            for nested_dataset_name, nested_dataset_items in nested_datasets_items.items():
                nested_dataset = cordra.CordraObject.create(obj_type="Dataset", obj_json=nested_dataset_properties | {"name": nested_dataset_name, "hasPart": nested_dataset_items}, **upload_kwargs)
                created_ids[nested_dataset["@id"]] = "Dataset"

            # rewrite created ids to only inclued files not present in the nested datasets
            for ids in nested_datasets_items.values():
                for id in ids:
                    del created_ids[id]

        logger.debug("Create workflow and action parameters")
        parameters = reconstructed_wfl.get("spec", {}).get("arguments", {}).get("parameters", [])
        for parameter in parameters:
            parameter_json = {
                "name": parameter["name"],
                "additionalType": "Text"  # All argo workflow input parameters are text parameters.
            }
            if "description" in parameter:
                parameter_json["description"] = parameter["description"]
            parameter_obj = cordra.CordraObject.create(
                obj_type="FormalParameter",
                obj_json=parameter_json,
                **upload_kwargs
            )
            created_ids[parameter_obj["@id"]] = "FormalParameter"
            if "value" in parameter:
                parameter_json["value"] = parameter["value"]
                property_obj = cordra.CordraObject.create(
                    obj_type="PropertyValue",
                    obj_json=parameter_json,
                    **upload_kwargs
                )
                created_ids[property_obj["@id"]] = "PropertyValue"

        logger.debug("Create ComputerLanguage")
        computer_language_obj = cordra.CordraObject.create(
            obj_type="ComputerLanguage",
            obj_json={
                "name": "Argo",
                "description": "Argo workflow language",
                "identifier": "https://argoproj.github.io/workflows",
                "url": "https://argoproj.github.io",
            },
            **upload_kwargs
        )
        created_ids[computer_language_obj["@id"]] = "ComputerLanguage"

        logger.debug("Create workflow")
        with tempfile.NamedTemporaryFile(delete=True,
                                         prefix=f"argo-workflow-tmp-") as tmp_file:
            yaml.dump(reconstructed_wfl, open(tmp_file.name, "w"), indent=2)
            tmp_file.flush()
            wfl_obj = cordra.CordraObject.create(
                obj_type="Workflow",
                obj_json={
                    "name": "workflow.yaml",
                    "contentSize": Path(tmp_file.name).stat().st_size,
                    "encodingFormat": "text/yaml",
                    "contentUrl": "workflow.yaml",
                    "description": "Argo workflow definition",
                    "programmingLanguage": computer_language_obj["@id"],
                    "input": [id for id, object_type in created_ids.items() if object_type == "FormalParameter"],
                },
                payloads={"workflow.yaml": ("workflow.yaml", open(tmp_file.name, "rb"))},
                **upload_kwargs
            )
            logger.debug("Workflow ingested")
            created_ids[wfl_obj["@id"]] = "Workflow"


        logger.debug("Create CreateAction")
        date_format = "%Y-%m-%dT%H:%M:%SZ"
        start_time = datetime.strptime(wfl["status"]["startedAt"], date_format)
        end_time = wfl["status"].get("finishedAt", None)
        if end_time is not None:
            end_time = datetime.strptime(end_time, date_format)
        else:
            for node_name in wfl["status"]["nodes"]:
                node_end_time = wfl["status"]["nodes"][node_name].get("finishedAt", None)
                if node_end_time is not None:
                    node_end_time = datetime.strptime(node_end_time, date_format)
                    if end_time is None or node_end_time > end_time:
                        end_time = node_end_time

        action_properties = {
            "result": [id for id in created_ids if created_ids[id] in ["FileObject", "Dataset"]],
            "startTime": start_time.strftime(date_format),
            "endTime": end_time.strftime(date_format),
            "instrument": wfl_obj["@id"],
            "object": [id for id, object_type in created_ids.items() if object_type == "PropertyValue"],
        }
        if create_action_author is not None:
            action_properties["agent"] = create_action_author["@id"]
        action = cordra.CordraObject.create(
            obj_type="CreateAction",
            obj_json=action_properties,
            **upload_kwargs
        )
        created_ids[action["@id"]] = "CreateAction"

        properties = {
            "name": wfl["metadata"].get("annotations", {}).get("workflows.argoproj.io/title", wfl["metadata"]["name"]),
            "description": wfl["metadata"].get("annotations", {}).get("workflows.argoproj.io/description", None),
            "author": [id for id in created_ids if created_ids[id] == "Person"],
            "hasPart": [id for id in created_ids if created_ids[id] in ["FileObject", "Workflow", "Dataset"]],
            "mentions": [action["@id"]],
            "mainEntity": wfl_obj["@id"],
            "license": workflow_annotations.get("argo-connector/license", None),
            "keywords": [keyword for keyword in workflow_annotations.get("argo-connector/keywords", "").split(",") if keyword != ""],
        }

        logger.debug("Create Dataset")
        dataset = cordra.CordraObject.create(obj_type="Dataset", obj_json=properties, **upload_kwargs)
        created_ids[dataset["@id"]] = "Dataset"

        # Update files parfOf/resultOf to point to dataset/action
        logger.debug("Updating files backref to dataset/action")
        for cordra_id in [id for id in created_ids if created_ids[id] in ["FileObject", "Dataset"] and id != dataset["@id"]]:
            obj = cordra.CordraObject.read(obj_id=cordra_id, **upload_kwargs)
            if ("isPartOf" not in obj) or (obj["isPartOf"] is None):
                obj["isPartOf"] = [dataset["@id"]]
            cordra.CordraObject.update(obj_id=cordra_id, obj_json=obj, **upload_kwargs)

        # touch root dataset, so it's newer than child datasets and files
        logger.debug("Touching root dataset")
        cordra.CordraObject.update(obj_id=dataset["@id"], obj_json=dataset, **upload_kwargs)

        logger.info(f"Dataset ingested. Cordra ID: {dataset['@id']} ({host}/#objects/{dataset['@id']})")
        return dataset["@id"]
    except Exception as e:
        print(f"Failed to create corda dataset: {type(e)} {str(e)}. Cleaning up uploaded objects")
        for cordra_id in created_ids:
            cordra.CordraObject.delete(obj_id=cordra_id, **upload_kwargs)
        raise e
