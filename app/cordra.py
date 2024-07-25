import os
from pathlib import Path
import tempfile
import magic
import cordra


def create_dataset_from_workflow_artifacts(host, user, password, wfl, artifact_stream_iterator):
    upload_kwargs = {
        "host": host,
        "username": user,
        "password": password,
        "verify": False
    }

    created_ids = {}
    try:
        # add authors
        print("Creating authors")
        author1 = cordra.CordraObject.create(obj_type="Person", obj_json={
            "name": "Daniel Bauer",
            "identifier": "https://orcid.org/0000-0001-9447-460X",
        }, **upload_kwargs)
        created_ids[author1["@id"]] = "Person"

        author2 = cordra.CordraObject.create(obj_type="Person", obj_json={
            "name": "Erik Kusch",
            "identifier": "https://orcid.org/0000-0002-4984-7646"
        }, **upload_kwargs)
        created_ids[author2["@id"]] = "Person"

        # upload files
        print("Creating file objects")
        for file_name, content_iterator in artifact_stream_iterator:

            print("Creating FileObject " + file_name)
            relative_path = file_name

            # Write file content into temporary file
            # Streaming the file directly into cordra would be better, but this didnt work for me.
            with tempfile.NamedTemporaryFile(delete=True,
                                             prefix=f"argo-artifact-tmp-{os.path.basename(file_name)}-") as tmp_file:
                print("Downloading content to temp file: " + tmp_file.name)
                try:
                    for chunk in content_iterator:
                        if chunk:
                            tmp_file.write(chunk)
                except Exception as e:
                    content_iterator.close()
                    raise e
                tmp_file.flush()
                file_size = Path(tmp_file.name).stat().st_size

                print(f"Data written to temp file {tmp_file.name} ({file_size / 1024 / 1024:.2f} MB)")

                # Cordra has issues with huge files
                if file_size / (1024 * 1024) > 1000:
                    print(f"File size is {file_size / 1024 / 1024:.2f} MB, which is too large to upload. Skipping...")
                    continue

                # figure out file encoding
                try:
                    encodingFormat = magic.from_file(tmp_file.name, mime=True)
                    print("Infered encoding format: " + encodingFormat)
                except magic.MagicException as e:
                    print(f"Failed to get encoding format for {file_name}")
                    encodingFormat = None

                # write object
                file_obj = cordra.CordraObject.create(
                    obj_type="FileObject",
                    obj_json={
                        "name": os.path.basename(relative_path),
                        "contentSize": Path(tmp_file.name).stat().st_size,
                        "encodingFormat": encodingFormat,
                        "contentUrl": relative_path,
                    },
                    payloads={relative_path: (relative_path, open(tmp_file.name, "rb"))},
                    **upload_kwargs
                )
            created_ids[file_obj["@id"]] = "FileObject"
            break  ## TODO remove to process multiple files

        # create action
        # TODO use workflow as action instead of software application
        print("Create Action")
        instrument = cordra.CordraObject.create(
            obj_type="SoftwareApplication",
            obj_json={
                "name": "ModGP",
                "identifier": "https://github.com/BioDT/uc-CWR"
            },
            **upload_kwargs
        )
        created_ids[instrument["@id"]] = "SoftwareApplication"

        action = cordra.CordraObject.create(
            obj_type="CreateAction",
            obj_json={
                "agent": author1["@id"],
                "result": [id for id in created_ids if created_ids[id] == "FileObject"],
                "instrument": instrument["@id"]
            },
            **upload_kwargs
        )
        created_ids[action["@id"]] = "CreateAction"

        print("Create Dataset")
        # TODO derive keywords, name and description from workflow input?
        dataset = cordra.CordraObject.create(obj_type="Dataset", obj_json={
            "name": "Species distribution models for <Enter Species>",
            "description": "ModGP workflow output for <Enter Species>",
            "keywords": ["GBIF", "Occurrence", "Biodiversity", "Observation", "ModGP", "SDM"],
            "license": "https://spdx.org/licenses/CC-BY-SA-2.0",
            "author": [author1["@id"], author2["@id"]],
            "hasPart": [id for id in created_ids if created_ids[id] == "FileObject"],
            "mentions": [action["@id"]],
        }, **upload_kwargs)
        created_ids[dataset["@id"]] = "Dataset"

        # Update files parfOf/resultOf to point to dataset/action
        print("Updating files backref to dataset/action")
        for id in [id for id in created_ids if created_ids[id] == "FileObject"]:
            obj = cordra.CordraObject.read(obj_id=id, **upload_kwargs)
            if ("partOf" not in obj) or (obj["partOf"] is None):
                obj["partOf"] = [dataset["@id"]]
            obj["resultOf"] = action["@id"]
            cordra.CordraObject.update(obj_id=id, obj_json=obj, **upload_kwargs)

    except Exception as e:
        print(f"Failed to create corda dataset: {type(e)} {str(e)}. Cleaning up uploaded objects")
        for id in created_ids:
            cordra.CordraObject.delete(obj_id=id, **upload_kwargs)