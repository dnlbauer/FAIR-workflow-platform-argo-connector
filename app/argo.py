import json
import sys
import uuid
from typing import Any, List

import argo_workflows
from argo_workflows.api import workflow_service_api
import requests
from bs4 import BeautifulSoup
import os
import urllib.parse


def _build_argo_client(url: str, token: str, verify_cert: bool = True):
    config = argo_workflows.Configuration(host=url,
                                          api_key_prefix={"BearerToken": f"Bearer {token}"},
                                          api_key={"BearerToken": "Bearer"})
    config.verify_ssl = verify_cert
    return argo_workflows.ApiClient(config)

def check_health(host: str, token: str, namespace: str, verify_cert: bool = True) -> bool|str:
    client = _build_argo_client(host, token, verify_cert=verify_cert)
    api = workflow_service_api.WorkflowServiceApi(client)
    try:
        api.list_workflows(namespace, list_options_limit="1", _check_return_type=False)
        return True
    except Exception as e:
        return str(e)


def get_workflow_information(host: str, token: str, namespace: str, workflow_name: str,
                             verify_cert: bool = True) -> dict[str: Any]:
    """ Return workflow information from Argo """
    client = _build_argo_client(host, token, verify_cert=verify_cert)
    api = workflow_service_api.WorkflowServiceApi(client)
    wfl = api.get_workflow(namespace, workflow_name, _check_return_type=False).to_dict()
    return wfl

def parse_artifact_list(wfl: dict[str: Any]) -> list[tuple[str, str, str]]:
    """ Returns a list of artifacts in the workflow
    Ignores artifacts that are not part of a nodes output folder. This should
    ensure that no caching data is archived.
    Ignores artifacts that are deleted after the workflow run is complete.
    """
    # find the last node in the node list
    artifacts_list = []
    for node_name in wfl["status"]["nodes"]:
        node = wfl["status"]["nodes"][node_name]

        if not "outputs" in node or not "artifacts" in node["outputs"]:
            continue

        artifacts = node["outputs"]["artifacts"]
        for artifact in artifacts:
            # ignore artifacts with a key outside of this wfl, since these are for caching
            if wfl["metadata"]["name"] not in artifact["s3"]["key"]:
                continue

            # ignore artifacts that are GCedd or planned to be GCed
            # these are intended for communication between steps
            if "deleted" in artifact and artifact["deleted"]:
                continue
            if "artifactGC" in artifact and artifact["artifactGC"]["strategy"] != "Never":
                continue

            if artifact["name"] == "main-logs":
                artifacts_list.append((node_name, artifact["name"], "main.log"))
            else:
                artifacts_list.append((node_name, artifact["name"], artifact["path"]))

    return artifacts_list

def reconstruct_workflow_from_workflowinfo(wfl: dict[str: Any]) -> dict[str: Any]:
    metadata = {
        "annotations": wfl["metadata"].get("annotations", {}),
    }

    spec = {}

    # if workflow is based on template. Copy steps from it
    if "workflowTemplateRef" in wfl["spec"]:
        for (key, value) in wfl["status"]["storedWorkflowTemplateSpec"].items():
            if key == "workflowTemplateRef":
                continue
            spec[key] = value

    # copy spec items. potentially overrides template spec elements
    for (key, value) in wfl["spec"].items():
        if key == "workflowTemplateRef":
            continue
        else:
            spec[key] = value

    reconstructed = {
        "kind": "Workflow",
        "metadata": metadata,
        "spec": spec,
    }

    return reconstructed

def _recursive_artifact_reader(url: str, argo_token: str, path: str, verify_cert: bool = True, chunk_size=1024 * 1024):
    headers = {"Authorization": f"Bearer {argo_token}"}

    # Ideally, we would do a HEAD request here to check if it is a Download.
    # However, agro does not properly support it. Head requests take a long time and fail for large files (gigabytes).
    # So we open a connection via a get request instead, and close the connection once we read the response headers.
    with requests.get(url, verify=verify_cert, headers=headers, stream=True) as response:
        if response.status_code != 200:
            response.raise_for_status()
        is_download = "Content-Disposition" in response.headers

    if is_download:
        # The file path from the API is not always the same as the file name of the download.
        # This is due to how Argo archives files. If the workflow stored the file in a compressed archive,
        # the API will return the path to the file while download will return the file in an archive format.
        # I.e. /tmp/my_file.txt might be returned as /tmp/my_file.zip.
        # In this case, we will rewrite the path to match the actual file downloaded from argo (the zip).
        file_name = response.headers.get("Content-Disposition").split("filename=")[1][1:-1]
        rewritten_path = os.path.join(os.path.dirname(path), file_name)
        print(f"Yielding file from {url} as {rewritten_path}")
        download_req = requests.get(url, verify=verify_cert, headers=headers, stream=True)
        download_req.raise_for_status()
        yield rewritten_path, download_req.iter_content(chunk_size=chunk_size)
    else:
        print("Downloading directory recursively: " + url)
        content = requests.get(url, verify=verify_cert, headers=headers).content
        soup = BeautifulSoup(content, features="html.parser")
        for link in soup.find_all("a"):
            href = link.get("href")
            if href == "..":
                continue
            new_url = urllib.parse.urljoin(url + "/", href)
            new_path = os.path.join(path, href)
            yield from _recursive_artifact_reader(new_url, argo_token, new_path, verify_cert, chunk_size)


def artifact_reader(host: str, token: str, namespace: str, workflow_name: str,
                    artifact_list: list[tuple[str, str, str]], verify_cert: bool = True):
    """ Returns a generator that yields the file_path and content steram of artifacts from Argo """
    for (node_id, artifact_name, file_path) in artifact_list:
        # build my own http request because the API submits "workflow" as discriminator where it should be "workflows".
        url = f"{host}/artifact-files/{namespace}/workflows/{workflow_name}/{node_id}/outputs/{artifact_name}"

        # build relative path for artifacts
        if file_path.startswith("/"):
            file_path = file_path[1:]
        path = os.path.join(node_id, file_path)

        yield from _recursive_artifact_reader(url, token, path, verify_cert)

def verify(host: str, token: str, workflow: dict[str: Any], namespace: str, verify_cert: bool = True) -> dict[str: Any]:
    """ Checks against agro to confirm this is a valid workflow. Returns the workflow definition if valid. """
    client = _build_argo_client(host, token, verify_cert=verify_cert)
    api = workflow_service_api.WorkflowServiceApi(client)

    wfl = workflow_service_api.IoArgoprojWorkflowV1alpha1Workflow(metadata=workflow.get("metadata", {}), spec=workflow.get("spec", {}), kind=workflow.get("kind", "Workflow"), _configuration=argo_workflows.configuration.Configuration(), _check_type=False)
    model = workflow_service_api.IoArgoprojWorkflowV1alpha1WorkflowLintRequest(namespace=namespace, workflow=wfl)
    response = api.lint_workflow(namespace, model, _check_return_type=False).to_dict()
    return response

def submit(host: str, token: str, workflow: dict[str: Any], namespace: str, dry_run: bool = False, verify_cert: bool = True):
    """ Submits a workflow to Argo """
    client = _build_argo_client(host, token, verify_cert=verify_cert)
    api = workflow_service_api.WorkflowServiceApi(client)

    # make sure name is deleted on resubmissions
    if "name" in workflow["metadata"]:
        del workflow["metadata"]["name"]

    # generate a random name if there is no name generator
    if not "generatedName" in workflow.get("metadata", {}):
        workflow["metadata"]["name"] = str(uuid.uuid4())

    wfl = workflow_service_api.IoArgoprojWorkflowV1alpha1Workflow(metadata=workflow["metadata"], spec=workflow["spec"], kind="Workflow", _configuration=argo_workflows.configuration.Configuration(), _check_type=False)
    model = workflow_service_api.IoArgoprojWorkflowV1alpha1WorkflowCreateRequest(namespace=namespace, workflow=wfl, kind="Workflow", server_dry_run=dry_run)
    return api.create_workflow(namespace, model, _check_return_type=False).to_dict

def list_workflows(host: str, token: str, verify_cert: bool = True) -> List[dict[str: Any]]:
    """ Lists workflows from Argo """
    client = _build_argo_client(host, token, verify_cert=verify_cert)
    api = workflow_service_api.WorkflowServiceApi(client)
    return api.list_workflows(namespace="argo", _check_return_type=False).to_dict()