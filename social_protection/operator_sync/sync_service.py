import logging
import uuid
from datetime import datetime

from django.db import transaction

from social_protection.models import BeneficiaryChangeLog
from social_protection.operator_sync.connector import BeneficiaryOperatorConnector

logger = logging.getLogger(__name__)


class BeneficiaryOperatorSyncService:
    def __init__(self, user):
        self.user = user
        self.connector = BeneficiaryOperatorConnector()

    def send_changes(
        self,
        change_ids=None,
        benefit_plan_id=None,
        only_pending=True,
        operations=None,
        include_failed=True,
    ):
        """
        Envoie les change logs sélectionnés à l'opérateur.
        Retourne un résumé {batch_id, sent, failed, skipped, results}.
        """
        qs = BeneficiaryChangeLog.objects.filter(is_deleted=False)
        if change_ids:
            qs = qs.filter(id__in=change_ids)
        if benefit_plan_id:
            qs = qs.filter(benefit_plan_id=benefit_plan_id)
        if operations:
            qs = qs.filter(operation__in=operations)
        if only_pending:
            statuses = [BeneficiaryChangeLog.SyncStatus.PENDING]
            if include_failed:
                statuses.append(BeneficiaryChangeLog.SyncStatus.FAILED)
            qs = qs.filter(sync_status__in=statuses)
        else:
            # Ne pas renvoyer les SENT sauf demande explicite via change_ids
            if not change_ids:
                qs = qs.exclude(sync_status=BeneficiaryChangeLog.SyncStatus.SENT)

        qs = qs.order_by("date_created")
        batch_id = uuid.uuid4()
        results = []
        sent = failed = skipped = 0

        for change in qs:
            if change.sync_status == BeneficiaryChangeLog.SyncStatus.SKIPPED:
                skipped += 1
                results.append({"id": str(change.id), "status": "SKIPPED"})
                continue

            ok, detail = self._send_one(change, batch_id)
            if ok:
                sent += 1
                results.append({"id": str(change.id), "status": "SENT", "response": detail})
            else:
                failed += 1
                results.append({"id": str(change.id), "status": "FAILED", "error": detail})

        return {
            "success": failed == 0,
            "batch_id": str(batch_id),
            "sent": sent,
            "failed": failed,
            "skipped": skipped,
            "results": results,
        }

    def _send_one(self, change: BeneficiaryChangeLog, batch_id):
        with transaction.atomic():
            change.sync_status = BeneficiaryChangeLog.SyncStatus.QUEUED
            change.sync_batch_id = batch_id
            change.sync_attempts = (change.sync_attempts or 0) + 1
            change.save(user=self.user)

        payload = change.payload or {}
        if not payload.get("requestId"):
            payload["requestId"] = str((change.json_ext or {}).get("request_id") or change.id)
            change.payload = payload
            change.save(user=self.user)

        try:
            response = self.connector.sync(payload)
            body = None
            try:
                body = response.json()
            except ValueError:
                body = {"raw": response.text}

            if response.status_code < 300 and (
                not isinstance(body, dict) or body.get("success", True) is not False
            ):
                change.sync_status = BeneficiaryChangeLog.SyncStatus.SENT
                change.synced_at = datetime.now()
                change.sync_error = {}
                change.operator_response = body if isinstance(body, dict) else {"raw": body}
                change.save(user=self.user)
                return True, body

            change.sync_status = BeneficiaryChangeLog.SyncStatus.FAILED
            change.sync_error = {
                "status_code": response.status_code,
                "body": body,
            }
            change.operator_response = body if isinstance(body, dict) else {"raw": body}
            change.save(user=self.user)
            return False, change.sync_error
        except Exception as exc:
            logger.exception("Operator sync failed for change=%s", change.id)
            change.sync_status = BeneficiaryChangeLog.SyncStatus.FAILED
            change.sync_error = {"error": str(exc)}
            change.save(user=self.user)
            return False, {"error": str(exc)}
