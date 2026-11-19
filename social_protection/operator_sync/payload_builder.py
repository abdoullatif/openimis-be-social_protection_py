import logging
from copy import deepcopy

from social_protection.apps import SocialProtectionConfig

logger = logging.getLogger(__name__)

# Champs techniques exclus du payload / du diff
TECHNICAL_FIELDS = {
    "id",
    "ID",
    "uuid",
    "report_synch",
    "version",
    "Unnamed: 0",
    "validations",
    "individual_id",
    "beneficiary_data_source",
    "code_menage",
    "codeMenage",
    "CodeMenage",
}

# Mapping champs internes → contrat opérateur (manuel API)
FIELD_ALIAS = {
    "first_name": "firstName",
    "last_name": "lastName",
    "dob": "dob",
    "sexe_bp": "sexe",
    "sexe": "sexe",
    "piece_didentite": "pieceIdentite",
    "tel_1": "tel1",
    "tel_2": "tel2",
    "location_code": "locationCode",
    "location_name": "locationName",
    "numero_paie": "numeroPaie",
}

# Découpage administratif toujours présent dans data (CREATE)
ADMIN_LOCATION_FIELDS = ("region", "prefecture", "sous_prefecture", "district")
BUSINESS_KEY = "id_ben_principal"


def _alias(key: str) -> str:
    return FIELD_ALIAS.get(key, key)


def extract_id_ben_principal(json_ext, fallback=None):
    if not isinstance(json_ext, dict):
        return fallback
    for key in ("id_ben_principal", "idBenPrincipal"):
        value = json_ext.get(key)
        if value not in (None, ""):
            return str(value)
    return fallback


def ensure_admin_locations(data, extra=None):
    """Garantit region / prefecture / sous_prefecture / district dans data."""
    data = dict(data or {})
    extra = extra or {}
    for key in ADMIN_LOCATION_FIELDS:
        value = data.get(key)
        if value in (None, ""):
            value = extra.get(key)
        data[key] = normalize_value(value) if value not in (None, "") else None
    return data


def normalize_value(value):
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def build_snapshot_from_beneficiary(beneficiary):
    """Construit un snapshot plat (clés opérateur) à partir d'un Beneficiary."""
    individual = beneficiary.individual
    data = {
        "firstName": individual.first_name if individual else None,
        "lastName": individual.last_name if individual else None,
        "dob": normalize_value(individual.dob) if individual else None,
    }
    if individual and individual.location:
        data["locationCode"] = individual.location.code
        data["locationName"] = individual.location.name

    json_parts = []
    if individual and isinstance(individual.json_ext, dict):
        json_parts.append(individual.json_ext)
    if isinstance(beneficiary.json_ext, dict):
        json_parts.append(beneficiary.json_ext)

    for part in json_parts:
        for key, value in part.items():
            if key in TECHNICAL_FIELDS:
                continue
            data[_alias(key)] = normalize_value(value)

    id_ben_principal = extract_id_ben_principal(
        beneficiary.json_ext,
        extract_id_ben_principal(individual.json_ext if individual else None),
    )
    if id_ben_principal:
        data[BUSINESS_KEY] = id_ben_principal
    return ensure_admin_locations(data)


def build_snapshot_from_row(row: dict):
    """Construit un snapshot à partir d'une ligne CSV (json_ext datasource)."""
    if not isinstance(row, dict):
        return {}
    data = {}
    for key, value in row.items():
        if key in TECHNICAL_FIELDS:
            continue
        data[_alias(key)] = normalize_value(value)
    id_ben_principal = extract_id_ben_principal(row)
    if id_ben_principal:
        data[BUSINESS_KEY] = id_ben_principal
    return ensure_admin_locations(data)


def compute_changed_fields(before: dict, after: dict):
    before = before or {}
    after = after or {}
    keys = set(before.keys()) | set(after.keys())
    changed = []
    for key in sorted(keys):
        if key in TECHNICAL_FIELDS or key == BUSINESS_KEY:
            # id_ben_principal est toujours à la racine du payload UPDATE
            if key == BUSINESS_KEY:
                continue
            if key in TECHNICAL_FIELDS:
                continue
        if before.get(key) != after.get(key):
            changed.append(key)
    return changed


def filter_sync_fields(data: dict):
    """Ne conserve que les champs configurés pour l'opérateur (si liste non vide)."""
    allowed = SocialProtectionConfig.operator_sync_fields or []
    if not allowed:
        return deepcopy(data or {})
    allowed_set = set(allowed)
    return {
        k: v
        for k, v in (data or {}).items()
        if k in allowed_set or k == BUSINESS_KEY or k in ADMIN_LOCATION_FIELDS
    }


def build_create_payload(request_id, id_ben_principal, beneficiary_id, data: dict):
    payload_data = ensure_admin_locations(filter_sync_fields(data))
    payload_data.pop(BUSINESS_KEY, None)
    return {
        "requestId": str(request_id),
        "operation": "CREATE",
        "id_ben_principal": id_ben_principal,
        "beneficiaryId": str(beneficiary_id) if beneficiary_id else None,
        "data": payload_data,
    }


def build_update_payload(request_id, id_ben_principal, beneficiary_id, changes: dict):
    filtered = filter_sync_fields(changes)
    filtered.pop(BUSINESS_KEY, None)
    return {
        "requestId": str(request_id),
        "operation": "UPDATE",
        "id_ben_principal": id_ben_principal,
        "beneficiaryId": str(beneficiary_id) if beneficiary_id else None,
        "changes": filtered,
    }


def build_delete_payload(request_id, id_ben_principal, beneficiary_id):
    return {
        "requestId": str(request_id),
        "operation": "DELETE",
        "id_ben_principal": id_ben_principal,
        "beneficiaryId": str(beneficiary_id) if beneficiary_id else None,
    }
