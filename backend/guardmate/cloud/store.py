"""Durable identities and reservations. No prompts, plans or resident profiles are stored."""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from pymongo import ReturnDocument
from pymongo.read_concern import ReadConcern
from pymongo.read_preferences import ReadPreference
from pymongo.write_concern import WriteConcern

ACCOUNT_ALLOWANCE = 1_000_000
SESSION_SECONDS = 900


class CloudError(Exception):
    def __init__(self, code: str, status_code: int = 409):
        self.code, self.status_code = code, status_code
        super().__init__(code)


def digest_token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class MongoCloudStore:
    def __init__(self, database, *, clock=None, transaction_runner=None):
        self.database = database
        self.clock = clock or (lambda: datetime.now(UTC))
        self._transaction_runner = transaction_runner
        self.accounts = database["cloud_accounts"]
        self.credentials = database["cloud_credentials"]
        self.sessions = database["cloud_sessions"]
        self.requests = database["cloud_requests"]
        self.flows = database["cloud_oauth_flows"]
        self.migrations = database["cloud_migrations"]

    def initialize(self):
        self.requests.create_index([("account_id", 1), ("request_id", 1)], unique=True)
        self.credentials.create_index("account_id")
        self.sessions.create_index("expires_at", expireAfterSeconds=0)
        self.flows.create_index("expires_at", expireAfterSeconds=0)
        # Account/request/credential identities deliberately have no TTL.

    def _transaction(self, callback):
        if self._transaction_runner is not None:
            return self._transaction_runner(callback)
        with self.database.client.start_session() as session:
            return session.with_transaction(
                callback,
                read_concern=ReadConcern("snapshot"),
                write_concern=WriteConcern(w="majority", wtimeout=5_000),
                read_preference=ReadPreference.PRIMARY,
                max_commit_time_ms=10_000,
            )

    @staticmethod
    def _options(session):
        return {"session": session} if session is not None else {}

    def ensure_account(self, google_subject: str):
        identity = digest_token("https://accounts.google.com|" + google_subject)
        return self.accounts.find_one_and_update(
            {"_id": identity},
            {
                "$setOnInsert": {
                    "allowance": ACCOUNT_ALLOWANCE,
                    "reserved": 0,
                    "created_at": self.clock(),
                    "disabled": False,
                }
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )

    def account(self, account_id):
        account = self.accounts.find_one({"_id": account_id, "disabled": False})
        if account is None:
            raise CloudError("account_unavailable", 401)
        return account

    def account_status(self, account_id):
        account = self.account(account_id)
        return {
            "account_id": account_id,
            "budget_microdollars": account["allowance"],
            "reserved_microdollars": account["reserved"],
            "remaining_microdollars": max(0, account["allowance"] - account["reserved"]),
        }

    def create_credential(self, account_id):
        self.account(account_id)
        token = "gm_activation_" + secrets.token_urlsafe(48)
        self.credentials.insert_one(
            {
                "_id": digest_token(token),
                "account_id": account_id,
                "revoked": False,
                "created_at": self.clock(),
            }
        )
        return token

    def revoke_credential(self, account_id, credential_id):
        return (
            self.credentials.update_one(
                {"_id": credential_id, "account_id": account_id}, {"$set": {"revoked": True}}
            ).matched_count
            == 1
        )

    def _credential(self, token):
        if not isinstance(token, str) or not 20 <= len(token) <= 4096:
            raise CloudError("invalid_credential", 401)
        credential = self.credentials.find_one({"_id": digest_token(token), "revoked": False})
        if credential is None:
            raise CloudError("invalid_credential", 401)
        self.account(credential["account_id"])
        return credential

    def create_session(self, account_id, *, kind="web", credential_id=None):
        self.account(account_id)
        token = "gm_session_" + secrets.token_urlsafe(48)
        self.sessions.insert_one(
            {
                "_id": digest_token(token),
                "account_id": account_id,
                "kind": kind,
                "credential_id": credential_id,
                "expires_at": self.clock() + timedelta(seconds=SESSION_SECONDS),
            }
        )
        return {"access_token": token, "account_id": account_id, "expires_in": SESSION_SECONDS}

    def activate(self, token):
        credential = self._credential(token)
        return self.create_session(
            credential["account_id"], kind="api", credential_id=credential["_id"]
        )

    def authenticate(self, token, *, kind):
        if not isinstance(token, str) or not 20 <= len(token) <= 4096:
            raise CloudError("sign_in_required", 401)
        session = self.sessions.find_one(
            {"_id": digest_token(token), "kind": kind, "expires_at": {"$gt": self.clock()}}
        )
        if session is None:
            raise CloudError("sign_in_required", 401)
        if (
            session["credential_id"] is not None
            and self.credentials.find_one({"_id": session["credential_id"], "revoked": False})
            is None
        ):
            raise CloudError("invalid_credential", 401)
        self.account(session["account_id"])
        return session["account_id"]

    def logout(self, token):
        self.sessions.delete_one({"_id": digest_token(token)})

    def request_record(self, account_id, request_id):
        return self.requests.find_one({"account_id": account_id, "request_id": request_id})

    @staticmethod
    def reject_duplicate(record, payload_digest):
        if record["payload_digest"] != payload_digest:
            raise CloudError("request_payload_changed")
        raise CloudError(
            "request_already_completed"
            if record["state"] == "completed"
            else "request_outcome_uncertain"
        )

    def reserve_and_claim(self, account_id, request_id, payload_digest, amount):
        if type(amount) is not int or not 0 < amount < 2**63:
            raise CloudError("invalid_reservation", 422)

        def admit(session):
            options = self._options(session)
            existing = self.requests.find_one(
                {"account_id": account_id, "request_id": request_id}, **options
            )
            if existing is not None:
                self.reject_duplicate(existing, payload_digest)
            result = self.accounts.update_one(
                {
                    "_id": account_id,
                    "disabled": False,
                    "$expr": {"$lte": [{"$add": ["$reserved", amount]}, "$allowance"]},
                },
                {"$inc": {"reserved": amount}},
                **options,
            )
            if result.matched_count != 1:
                raise CloudError("account_budget_exhausted", 402)
            self.requests.insert_one(
                {
                    "account_id": account_id,
                    "request_id": request_id,
                    "payload_digest": payload_digest,
                    "reserved": amount,
                    "state": "dispatching",
                    "created_at": self.clock(),
                },
                **options,
            )
            return True

        return self._transaction(admit)

    def finish_request(self, account_id, request_id, *, completed):
        result = self.requests.update_one(
            {"account_id": account_id, "request_id": request_id, "state": "dispatching"},
            {
                "$set": {
                    "state": "completed" if completed else "uncertain",
                    "finished_at": self.clock(),
                }
            },
        )
        if result.matched_count != 1:
            raise CloudError("request_outcome_uncertain")

    def preserve_legacy_spend(self, account_id, migration_id, reserved):
        """One explicit legacy-account opening balance, never reset or replicate across users."""
        if type(reserved) is not int or reserved < 0 or reserved >= 2**63:
            raise CloudError("invalid_legacy_reservation", 422)

        def migrate(session):
            options = self._options(session)
            marker = self.migrations.find_one({"_id": migration_id}, **options)
            if marker is not None:
                if marker["account_id"] != account_id or marker["reserved"] != reserved:
                    raise CloudError("legacy_migration_conflict")
                return True
            existing = self.accounts.find_one({"_id": account_id}, **options)
            if existing is None:
                raise CloudError("account_unavailable", 401)
            prior = existing.get("legacy_migration")
            if prior is not None and prior != migration_id:
                raise CloudError("legacy_migration_conflict")
            if prior is None:
                self.accounts.update_one(
                    {"_id": account_id},
                    {
                        "$inc": {"reserved": reserved},
                        "$set": {
                            "legacy_migration": migration_id,
                            "legacy_opening_balance": reserved,
                        },
                    },
                    **options,
                )
            elif existing.get("legacy_opening_balance") != reserved:
                raise CloudError("legacy_migration_conflict")
            self.migrations.insert_one(
                {
                    "_id": migration_id,
                    "account_id": account_id,
                    "reserved": reserved,
                    "created_at": self.clock(),
                },
                **options,
            )
            return True

        return self._transaction(migrate)
