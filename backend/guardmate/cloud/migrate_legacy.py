"""Explicit legacy reservation migration; dry-run by default and never loads .env."""

import argparse
import json

from ..mongo import MongoPersistence
from .store import MongoCloudStore


def migrate(store, account_id, *, apply=False):
    account = store.account(account_id)
    original = store.database["model_budget"].find_one({"_id": 1})
    amount = original["reserved"] if original is not None else 0
    if type(amount) is not int or not 0 <= amount < 2**63:
        raise ValueError("Invalid historical reservation total.")
    marker_id = "original-shared-inference-budget-v1"
    marker = store.migrations.find_one({"_id": marker_id})
    if marker is not None and marker["account_id"] != account_id:
        raise ValueError("Historical spend is already assigned to another account.")
    if marker is not None and marker["reserved"] != amount:
        raise ValueError(
            "Historical ledger changed after migration; manual reconciliation required."
        )
    if apply:
        store.preserve_legacy_spend(account_id, marker_id, amount)
    return {
        "applied": apply,
        "already_migrated": marker is not None,
        "legacy_reserved_microdollars": amount,
        "account_reserved_microdollars": store.account(account_id)["reserved"],
        "additional_opening_microdollars": 0 if marker is not None else amount,
        "budget_microdollars": account["allowance"],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    connection = None
    try:
        connection = MongoPersistence.from_environment()
        if connection is None:
            raise ValueError("MongoDB is not configured.")
        store = MongoCloudStore(connection.database)
        result = migrate(store, args.account_id, apply=args.apply)
    except Exception:
        parser.exit(1, "Legacy migration unavailable; check account, ledger and configuration.\n")
    finally:
        if connection is not None:
            connection.close()
    print(json.dumps(result))


if __name__ == "__main__":
    main()
