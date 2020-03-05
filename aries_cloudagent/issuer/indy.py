"""Indy issuer implementation."""

import json
import logging
from typing import Sequence, Tuple

import indy.anoncreds
import indy.blob_storage
from indy.error import AnoncredsRevocationRegistryFullError, IndyError, ErrorCode

from ..messaging.util import encode

from .base import (
    BaseIssuer,
    IssuerError,
    IssuerRevocationRegistryFullError,
    DEFAULT_CRED_DEF_TAG,
    DEFAULT_ISSUANCE_TYPE,
    DEFAULT_SIGNATURE_TYPE,
)
from ..indy.error import IndyErrorHandler


class IndyIssuer(BaseIssuer):
    """Indy issuer class."""

    def __init__(self, wallet):
        """
        Initialize an IndyIssuer instance.

        Args:
            wallet: IndyWallet instance

        """
        self.logger = logging.getLogger(__name__)
        self.wallet = wallet

    def make_schema_id(
        self, origin_did: str, schema_name: str, schema_version: str
    ) -> str:
        """Derive the ID for a schema."""
        return f"{origin_did}:2:{schema_name}:{schema_version}"

    async def create_and_store_schema(
        self,
        origin_did: str,
        schema_name: str,
        schema_version: str,
        attribute_names: Sequence[str],
    ) -> Tuple[str, str]:
        """
        Create a new credential schema and store it in the wallet.

        Args:
            origin_did: the DID issuing the credential definition
            schema_name: the schema name
            schema_version: the schema version
            attribute_names: a sequence of schema attribute names

        Returns:
            A tuple of the schema ID and JSON

        """

        with IndyErrorHandler("Error when creating schema", IssuerError):
            schema_id, schema_json = await indy.anoncreds.issuer_create_schema(
                origin_did, schema_name, schema_version, json.dumps(attribute_names),
            )
        return (schema_id, schema_json)

    def make_credential_definition_id(
        self, origin_did: str, schema: dict, signature_type: str = None, tag: str = None
    ) -> str:
        """Derive the ID for a credential definition."""
        signature_type = signature_type or DEFAULT_SIGNATURE_TYPE
        tag = tag or DEFAULT_CRED_DEF_TAG
        return f"{origin_did}:3:{signature_type}:{str(schema['seqNo'])}:{tag}"

    async def credential_definition_in_wallet(
        self, credential_definition_id: str
    ) -> bool:
        """
        Check whether a given credential definition ID is present in the wallet.

        Args:
            credential_definition_id: The credential definition ID to check
        """
        try:
            await indy.anoncreds.issuer_create_credential_offer(
                self.wallet.handle, credential_definition_id
            )
            return True
        except IndyError as error:
            if error.error_code not in (
                ErrorCode.CommonInvalidStructure,
                ErrorCode.WalletItemNotFound,
            ):
                raise IndyErrorHandler.wrap_error(
                    error,
                    "Error when checking wallet for credential definition",
                    IssuerError,
                ) from error
            # recognized error signifies no such cred def in wallet: pass
        return False

    async def create_and_store_credential_definition(
        self,
        origin_did: str,
        schema: dict,
        signature_type: str = None,
        tag: str = None,
        support_revocation: bool = False,
    ) -> Tuple[str, str]:
        """
        Create a new credential definition and store it in the wallet.

        Args:
            origin_did: the DID issuing the credential definition
            schema_json: the schema used as a basis
            signature_type: the credential definition signature type (default 'CL')
            tag: the credential definition tag
            support_revocation: whether to enable revocation for this credential def

        Returns:
            A tuple of the credential definition ID and JSON

        """

        with IndyErrorHandler("Error when creating credential definition", IssuerError):
            (
                credential_definition_id,
                credential_definition_json,
            ) = await indy.anoncreds.issuer_create_and_store_credential_def(
                self.wallet.handle,
                origin_did,
                json.dumps(schema),
                tag or DEFAULT_CRED_DEF_TAG,
                signature_type or DEFAULT_SIGNATURE_TYPE,
                json.dumps({"support_revocation": support_revocation}),
            )
        return (credential_definition_id, credential_definition_json)

    async def create_credential_offer(self, credential_definition_id: str):
        """
        Create a credential offer for the given credential definition id.

        Args:
            credential_definition_id: The credential definition to create an offer for

        Returns:
            A credential offer

        """
        with IndyErrorHandler("Exception when creating credential offer", IssuerError):
            credential_offer_json = await indy.anoncreds.issuer_create_credential_offer(
                self.wallet.handle, credential_definition_id
            )

        credential_offer = json.loads(credential_offer_json)

        return credential_offer

    async def create_credential(
        self,
        schema,
        credential_offer,
        credential_request,
        credential_values,
        revoc_reg_id: str = None,
        tails_reader_handle: int = None,
    ):
        """
        Create a credential.

        Args
            schema: Schema to create credential for
            credential_offer: Credential Offer to create credential for
            credential_request: Credential request to create credential for
            credential_values: Values to go in credential
            revoc_reg_id: ID of the revocation registry
            tails_reader_handle: Handle for the tails file blob reader

        Returns:
            A tuple of created credential, revocation id

        """

        encoded_values = {}
        schema_attributes = schema["attrNames"]
        for attribute in schema_attributes:
            # Ensure every attribute present in schema to be set.
            # Extraneous attribute names are ignored.
            try:
                credential_value = credential_values[attribute]
            except KeyError:
                raise IssuerError(
                    "Provided credential values are missing a value "
                    + f"for the schema attribute '{attribute}'"
                )

            encoded_values[attribute] = {}
            encoded_values[attribute]["raw"] = str(credential_value)
            encoded_values[attribute]["encoded"] = encode(credential_value)

        try:
            (
                credential_json,
                credential_revocation_id,
                revoc_reg_delta_json,
            ) = await indy.anoncreds.issuer_create_credential(
                self.wallet.handle,
                json.dumps(credential_offer),
                json.dumps(credential_request),
                json.dumps(encoded_values),
                revoc_reg_id,
                tails_reader_handle,
            )
        except AnoncredsRevocationRegistryFullError:
            self.logger.error("Revocation registry is full when creating a credential.")
            raise IssuerRevocationRegistryFullError("Revocation registry full")
        except IndyError as error:
            raise IndyErrorHandler.wrap_error(
                error, "Error when issuing credential", IssuerError
            ) from error

        return json.loads(credential_json), credential_revocation_id

    async def revoke_credential(
        self, revoc_reg_id: str, tails_reader_handle: int, cred_revoc_id: str
    ) -> dict:
        """
        Revoke a credential.

        Args
            revoc_reg_id: ID of the revocation registry
            tails_reader_handle: handle for the registry tails file
            cred_revoc_id: index of the credential in the revocation registry

        """
        with IndyErrorHandler("Exception when revoking credential", IssuerError):
            revoc_reg_delta_json = await indy.anoncreds.issuer_revoke_credential(
                self.wallet.handle, tails_reader_handle, revoc_reg_id, cred_revoc_id
            )
            # may throw AnoncredsInvalidUserRevocId if using ISSUANCE_ON_DEMAND

        delta = json.loads(revoc_reg_delta_json)

        return delta

    async def create_and_store_revocation_registry(
        self,
        origin_did: str,
        cred_def_id: str,
        revoc_def_type: str,
        tag: str,
        max_cred_num: int,
        tails_base_path: str,
        issuance_type: str = None,
    ) -> Tuple[str, str, str]:
        """
        Create a new revocation registry and store it in the wallet.

        Args:
            origin_did: the DID issuing the revocation registry
            cred_def_id: the identifier of the related credential definition
            revoc_def_type: the revocation registry type (default CL_ACCUM)
            tag: the unique revocation registry tag
            max_cred_num: the number of credentials supported in the registry
            tails_base_path: where to store the tails file
            issuance_type: optionally override the issuance type

        Returns:
            A tuple of the revocation registry ID, JSON, and entry JSON

        """

        tails_writer_config = json.dumps(
            {"base_dir": tails_base_path, "uri_pattern": ""}
        )
        tails_writer = await indy.blob_storage.open_writer(
            "default", tails_writer_config
        )

        with IndyErrorHandler(
            "Exception when creating revocation registry", IssuerError
        ):
            (
                revoc_reg_id,
                revoc_reg_def_json,
                revoc_reg_entry_json,
            ) = await indy.anoncreds.issuer_create_and_store_revoc_reg(
                self.wallet.handle,
                origin_did,
                revoc_def_type,
                tag,
                cred_def_id,
                json.dumps(
                    {
                        "max_cred_num": max_cred_num,
                        "issuance_type": issuance_type or DEFAULT_ISSUANCE_TYPE,
                    }
                ),
                tails_writer,
            )
        return (revoc_reg_id, revoc_reg_def_json, revoc_reg_entry_json)
