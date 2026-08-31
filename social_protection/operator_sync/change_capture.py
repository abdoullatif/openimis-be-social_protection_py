import logging
import uuid

from individual.models import IndividualDataSource
from social_protection.models import (
    Beneficiary,
    BeneficiaryChangeLog,
    BenefitPlanDataUploadRecords,
)
from social_protection.operator_sync.payload_builder import (
    build_create_payload,
    build_snapshot_from_beneficiary,
    build_snapshot_from_row,
    build_update_payload,
    compute_changed_fields,
    extract_code_menage,
)

logger = logging.getLogger(__name__)


def _is_valid_source(source: IndividualDataSource) -> bool:
    validations = source.validations or {}
    errors = validations.get("validation_errors", [])
    if errors in ([], None, "[]"):
        return True
    if isinstance(errors, str):
        return errors.strip() in ("", "[]")
    return False


def _get_upload_record(upload_uuid, benefit_plan):
    return BenefitPlanDataUploadRecords.objects.filter(
        data_upload_id=upload_uuid,
        benefit_plan=benefit_plan,
        is_deleted=False,
    ).first()


def _is_update_workflow(workflow_name: str) -> bool:
    if not workflow_name:
        return False
    return "update" in workflow_name.lower()


def capture_changes_from_upload(user, benefit_plan, upload_uuid, accepted=None):
    """
    Enregistre un BeneficiaryChangeLog par ligne CSV traitée avec succès.
    À appeler après un workflow Valid Upload / Valid Update.
    """
    upload_record = _get_upload_record(upload_uuid, benefit_plan)
    if not upload_record:
        logger.warning(
            "No BenefitPlanDataUploadRecords for upload=%s plan=%s",
            upload_uuid,
            benefit_plan.id,
        )
        return []

    is_update = _is_update_workflow(upload_record.workflow)
    sources = IndividualDataSource.objects.filter(
        upload_id=upload_uuid,
        is_deleted=False,
    )
    if accepted:
        sources = sources.filter(id__in=accepted)

    created_logs = []
    for source in sources:
        if not _is_valid_source(source):
            continue

        row = source.json_ext or {}
        try:
            if is_update:
                log = _capture_update(user, benefit_plan, upload_record, source, row)
            else:
                log = _capture_create(user, benefit_plan, upload_record, source, row)
            if log:
                created_logs.append(log)
        except Exception:
            logger.exception(
                "Failed to capture change log for datasource=%s upload=%s",
                source.id,
                upload_uuid,
            )
    return created_logs


def _already_logged(upload_record, beneficiary_id, operation):
    return BeneficiaryChangeLog.objects.filter(
        upload_record=upload_record,
        beneficiary_id=beneficiary_id,
        operation=operation,
        is_deleted=False,
    ).exists()


def _capture_create(user, benefit_plan, upload_record, source, row):
    beneficiary = None
    if source.individual_id:
        beneficiary = Beneficiary.objects.filter(
            benefit_plan=benefit_plan,
            individual_id=source.individual_id,
            is_deleted=False,
        ).first()

    if not beneficiary:
        # Fallback: parfois l'ID est déjà dans le CSV (rare en create)
        beneficiary_id = row.get("ID") or row.get("id")
        if beneficiary_id:
            beneficiary = Beneficiary.objects.filter(
                id=beneficiary_id,
                benefit_plan=benefit_plan,
                is_deleted=False,
            ).first()

    if not beneficiary:
        logger.warning("CREATE capture: beneficiary not found for source=%s", source.id)
        return None

    if _already_logged(upload_record, beneficiary.id, BeneficiaryChangeLog.Operation.CREATE):
        return None

    snapshot = build_snapshot_from_beneficiary(beneficiary)
    if not snapshot:
        snapshot = build_snapshot_from_row(row)

    code_menage = extract_code_menage(snapshot, extract_code_menage(row))
    request_id = uuid.uuid4()
    payload = build_create_payload(request_id, code_menage, beneficiary.id, snapshot)

    log = BeneficiaryChangeLog(
        benefit_plan=benefit_plan,
        beneficiary=beneficiary,
        individual=beneficiary.individual,
        upload_record=upload_record,
        operation=BeneficiaryChangeLog.Operation.CREATE,
        source=BeneficiaryChangeLog.Source.CSV_UPLOAD,
        code_menage=code_menage,
        snapshot_before={},
        snapshot_after=snapshot,
        changed_fields=[],
        payload=payload,
        sync_status=BeneficiaryChangeLog.SyncStatus.PENDING,
        json_ext={"request_id": str(request_id)},
    )
    log.save(user=user)
    return log


def _capture_update(user, benefit_plan, upload_record, source, row):
    beneficiary_id = row.get("ID") or row.get("id")
    if not beneficiary_id:
        logger.warning("UPDATE capture: missing ID in source=%s", source.id)
        return None

    beneficiary = Beneficiary.objects.filter(
        id=beneficiary_id,
        benefit_plan=benefit_plan,
        is_deleted=False,
    ).first()
    if not beneficiary:
        logger.warning(
            "UPDATE capture: beneficiary %s not found for plan %s",
            beneficiary_id,
            benefit_plan.id,
        )
        return None

    if _already_logged(upload_record, beneficiary.id, BeneficiaryChangeLog.Operation.UPDATE):
        return None

    after = build_snapshot_from_beneficiary(beneficiary)
    before = {}
    # Diff via simple_history si disponible
    try:
        history = list(beneficiary.history.all()[:2])
        if len(history) >= 2:
            prev = history[1]
            # reconstruit un snapshot minimal depuis l'historique beneficiary + individual
            before = build_snapshot_from_beneficiary(beneficiary)
            # Remplace json_ext/status depuis prev si possible
            if hasattr(prev, "json_ext") and isinstance(prev.json_ext, dict):
                from social_protection.operator_sync.payload_builder import build_snapshot_from_row
                # utilise l'état après pour les champs individual, et prev.json_ext pour beneficiary ext
                before = dict(after)
                for k, v in build_snapshot_from_row(prev.json_ext or {}).items():
                    before[k] = v
            # Individual history
            individual = beneficiary.individual
            if individual:
                ind_hist = list(individual.history.all()[:2])
                if len(ind_hist) >= 2:
                    prev_ind = ind_hist[1]
                    before["firstName"] = prev_ind.first_name
                    before["lastName"] = prev_ind.last_name
                    before["dob"] = (
                        prev_ind.dob.isoformat() if getattr(prev_ind, "dob", None) else None
                    )
                    if isinstance(prev_ind.json_ext, dict):
                        from social_protection.operator_sync.payload_builder import (
                            build_snapshot_from_row as _row,
                        )
                        for k, v in _row(prev_ind.json_ext).items():
                            before[k] = v
    except Exception:
        logger.exception("Could not compute before snapshot for beneficiary=%s", beneficiary.id)
        before = {}

    # Si pas d'historique fiable: considère les champs présents dans le CSV comme candidats
    if not before:
        row_snapshot = build_snapshot_from_row(row)
        changed_fields = [k for k in row_snapshot.keys() if k != "codeMenage"]
        changes = {k: after.get(k, row_snapshot.get(k)) for k in changed_fields}
    else:
        changed_fields = compute_changed_fields(before, after)
        changes = {k: after.get(k) for k in changed_fields}

    code_menage = extract_code_menage(after, extract_code_menage(row))
    request_id = uuid.uuid4()
    payload = build_update_payload(request_id, code_menage, beneficiary.id, changes)

    log = BeneficiaryChangeLog(
        benefit_plan=benefit_plan,
        beneficiary=beneficiary,
        individual=beneficiary.individual,
        upload_record=upload_record,
        operation=BeneficiaryChangeLog.Operation.UPDATE,
        source=BeneficiaryChangeLog.Source.CSV_UPDATE,
        code_menage=code_menage,
        snapshot_before=before,
        snapshot_after=after,
        changed_fields=changed_fields,
        payload=payload,
        sync_status=BeneficiaryChangeLog.SyncStatus.PENDING,
        json_ext={"request_id": str(request_id)},
    )
    log.save(user=user)
    return log


def capture_delete_from_beneficiary(user, beneficiary, source=BeneficiaryChangeLog.Source.GRAPHQL):
    """Capture une suppression (soft-delete) pour sync opérateur."""
    from social_protection.operator_sync.payload_builder import build_delete_payload

    snapshot = build_snapshot_from_beneficiary(beneficiary)
    code_menage = extract_code_menage(snapshot)
    request_id = uuid.uuid4()
    payload = build_delete_payload(request_id, code_menage, beneficiary.id)

    log = BeneficiaryChangeLog(
        benefit_plan=beneficiary.benefit_plan,
        beneficiary=beneficiary,
        individual=beneficiary.individual,
        upload_record=None,
        operation=BeneficiaryChangeLog.Operation.DELETE,
        source=source,
        code_menage=code_menage,
        snapshot_before=snapshot,
        snapshot_after={},
        changed_fields=[],
        payload=payload,
        sync_status=BeneficiaryChangeLog.SyncStatus.PENDING,
        json_ext={"request_id": str(request_id)},
    )
    log.save(user=user)
    return log
