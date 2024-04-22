from datetime import datetime
from pathlib import Path
from typing import Literal, Optional, Union

from bioimageio.spec.model.v0_5 import WeightsFormat
from loguru import logger

from ._settings import settings
from .backup import ZenodoHost, backup
from .db_structure.chat import Chat, Message
from .generate_collection_json import generate_collection_json
from .gh_utils import set_gh_actions_outputs
from .mailroom import notify_uploader
from .remote_resource import (
    PublishedVersion,
    ResourceConcept,
    get_remote_resource_version,
)
from .run_dynamic_tests import run_dynamic_tests
from .s3_client import Client
from .validate_format import validate_format


class BackOffice:
    """This backoffice aids to maintain the bioimage.io collection"""

    def __init__(
        self,
        host: str = settings.s3_host,
        bucket: str = settings.s3_bucket,
        prefix: str = settings.s3_folder,
    ) -> None:
        super().__init__()
        self.client = Client(host=host, bucket=bucket, prefix=prefix)
        logger.info("created backoffice with client {}", self.client)

    def wipe(self, subfolder: str = ""):
        """DANGER ZONE: wipes `subfolder` completely, only use for test folders!"""
        url = self.client.get_file_url(subfolder)
        key_parts = ("sandbox", "testing")
        if not all(p in url for p in key_parts):
            raise RuntimeError(f"Refusing to wipe {url} (missing {key_parts})")

        self.client.rm_dir(subfolder)

    def stage(self, resource_id: str, package_url: str):
        """stage a new resourse (version) from `package_url`"""
        resource = ResourceConcept(self.client, resource_id)
        staged = resource.stage_new_version(package_url)
        set_gh_actions_outputs(version=staged.version)

    def validate_format(self, resource_id: str, version: str):
        """validate a (staged) resource version's bioimageio.yaml"""
        rv = get_remote_resource_version(self.client, resource_id, version)
        dynamic_test_cases, conda_envs = validate_format(rv)
        set_gh_actions_outputs(
            has_dynamic_test_cases=bool(dynamic_test_cases),
            dynamic_test_cases={"include": dynamic_test_cases},
            conda_envs=conda_envs,
        )

    def test(
        self,
        resource_id: str,
        version: str,
        weight_format: Optional[Union[WeightsFormat, Literal[""]]] = None,
        create_env_outcome: Literal["success", ""] = "success",
    ):
        """run dynamic tests for a (staged) resource version"""
        rv = get_remote_resource_version(self.client, resource_id, version)
        if isinstance(rv, PublishedVersion):
            raise ValueError(
                f"Testing of already published {resource_id} {version} is not implemented"
            )

        run_dynamic_tests(
            staged=rv,
            weight_format=weight_format or None,
            create_env_outcome=create_env_outcome,
        )

    def await_review(self, resource_id: str, version: str):
        """mark a (staged) resource version is awaiting review"""
        rv = get_remote_resource_version(self.client, resource_id, version)
        if isinstance(rv, PublishedVersion):
            raise ValueError(
                f"Cannot await review for already published {resource_id} {version}"
            )
        rv.await_review()
        notify_uploader(
            rv,
            "is awaiting review ⌛",
            f"Thank you for proposing {rv.id} {rv.version}!\n"
            + "Our maintainers will take a look shortly!",
        )

    def request_changes(
        self, resource_id: str, version: str, reviewer: str, reason: str
    ):
        """mark a (staged) resource version as needing changes"""
        rv = get_remote_resource_version(self.client, resource_id, version)
        if isinstance(rv, PublishedVersion):
            raise ValueError(
                f"Requesting changes of already published  {resource_id} {version} is not implemented"
            )

        rv.request_changes(reviewer=reviewer, reason=reason)
        notify_uploader(
            rv,
            "needs changes 📑",
            f"Thank you for proposing {rv.id} {rv.version}!\n"
            + "We kindly ask you to upload an updated version, because: \n"
            + f"{reason}\n",
        )

    def publish(self, resource_id: str, version: str, reviewer: str):
        """publish a (staged) resource version"""
        rv = get_remote_resource_version(self.client, resource_id, version)
        if isinstance(rv, PublishedVersion):
            raise ValueError(
                f"Cannot publish already published {resource_id} {version}"
            )

        try:
            rv.lock_publish()
            published: PublishedVersion = rv.publish(reviewer=reviewer)
            assert isinstance(published, PublishedVersion)
            self.generate_collection_json()
            notify_uploader(
                rv,
                "was published! 🎉",
                f"Thank you for contributing {published.id} {published.version} to bioimage.io!\n"
                + "Check it out at https://bioimage.io/#/?id={published.id}\n",  # TODO: link to version
            )
        finally:
            rv.unlock_publish()

    def backup(self, destination: ZenodoHost):
        """backup the whole collection (to zenodo.org)"""
        _ = backup(self.client, destination)

    def generate_collection_json(
        self, collection_template: Path = Path("collection_template.json")
    ):
        """generate the collection.json file --- a summary of the whole collection"""
        generate_collection_json(self.client, collection_template=collection_template)

    def forward_emails_to_chat(self):
        logger.error("disabled")
        # forward_emails_to_chat(self.client, last_n_days=7)

    def add_chat_message(
        self, resource_id: str, version: str, chat_message: str, author: str
    ):
        chat = Chat(
            messages=[
                Message(author=author, text=chat_message, timestamp=datetime.now())
            ]
        )
        rv = get_remote_resource_version(self.client, resource_id, version)
        rv.extend_chat(chat)
