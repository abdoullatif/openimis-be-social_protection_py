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
    "code_menage": "codeMenage",
}


def _alias(key: str) -> str:
    return FIELD_ALIAS.get(key, key)


def extract_code_menage(json_ext, fallback=None):
    if not isinstance(json_ext, dict):
        return fallback
    for key in ("code_menage", "codeMenage", "CodeMenage"):
        value = json_ext.get(key)
        if value not in (None, ""):
            return str(value)
    return fallback


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

    code_menage = extract_code_menage(
        beneficiary.json_ext,
        extract_code_menage(individual.json_ext if individual else None),
    )
    if code_menage:
        data["codeMenage"] = code_menage
    return data


def build_snapshot_from_row(row: dict):
    """Construit un snapshot à partir d'une ligne CSV (json_ext datasource)."""
    if not isinstance(row, dict):
        return {}
    data = {}
    for key, value in row.items():
        if key in TECHNICAL_FIELDS:
            continue
        data[_alias(key)] = normalize_value(value)
    code_menage = extract_code_menage(row)
    if code_menage:
        data["codeMenage"] = code_menage
    return data


def compute_changed_fields(before: dict, after: dict):
    before = before or {}
    after = after or {}
    keys = set(before.keys()) | set(after.keys())
    changed = []
    for key in sorted(keys):
        if key in TECHNICAL_FIELDS or key == "codeMenage":
            # codeMenage toujours présent dans le payload UPDATE, pas listé comme "change"
            if key == "codeMenage":
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
    return {k: v for k, v in (data or {}).items() if k in allowed_set or k == "codeMenage"}


def build_create_payload(request_id, code_menage, beneficiary_id, data: dict):
    return {
        "requestId": str(request_id),
        "operation": "CREATE",
        "codeMenage": code_menage,
        "beneficiaryId": str(beneficiary_id) if beneficiary_id else None,
        "data": filter_sync_fields(data),
    }


def build_update_payload(request_id, code_menage, beneficiary_id, changes: dict):
    filtered = filter_sync_fields(changes)
    # codeMenage ne doit pas être dans changes
    filtered.pop("codeMenage", None)
    return {
        "requestId": str(request_id),
        "operation": "UPDATE",
        "codeMenage": code_menage,
        "beneficiaryId": str(beneficiary_id) if beneficiary_id else None,
        "changes": filtered,
    }


def build_delete_payload(request_id, code_menage, beneficiary_id):
    return {
        "requestId": str(request_id),
        "operation": "DELETE",
        "codeMenage": code_menage,
        "beneficiaryId": str(beneficiary_id) if beneficiary_id else None,
    }
