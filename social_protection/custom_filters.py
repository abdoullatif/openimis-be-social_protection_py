import logging
import re
import json

from collections import namedtuple
from django.db.models import Q
from django.db.models.query import QuerySet
from typing import Any, List, Optional

from core.custom_filters import CustomFilterWizardInterface, CustomFilterWizardStorage
from core.custom_filters.filter_condition_utils import (
    extract_custom_filters_from_json_ext,
    parse_custom_filter_part,
)
from social_protection.models import BenefitPlan, Beneficiary, BeneficiaryStatus

CUSTOM_FILTER_MODULE = "social_protection"


logger = logging.getLogger(__name__)


class BenefitPlanCustomFilterWizard(CustomFilterWizardInterface):

    OBJECT_CLASS = BenefitPlan

    def get_type_of_object(self) -> str:
        """
        Get the type of object for which we want to define a specific way of building filters.

        :return: The type of the object.
        :rtype: str
        """
        return self.OBJECT_CLASS.__name__

    def load_definition(self, tuple_type: type, **kwargs) -> List[namedtuple]:
        """
        Load the definition of how to create filters.

        This method retrieves the definition of how to create filters and returns it as a list of named tuples.
        Each named tuple is built with the provided `tuple_type` and has the fields `field`, `filter`, and `value`.

        Example named tuple: <Type>(field=<str>, filter=<str>, type=<str>)
        Example usage: BenefitPlan(field='income', filter='lt, gte, icontains, exact', type='integer')

        :param tuple_type: The type of the named tuple.
        :type tuple_type: type

        :return: A list of named tuples representing the definition of how to create filters.
        :rtype: List[namedtuple]
        """
        benefit_plan_id = kwargs.get('uuid', None)
        additional_params = kwargs.get('additional_params', None)
        if not benefit_plan_id and isinstance(additional_params, dict):
            benefit_plan_id = (
                additional_params.get('benefitPlan')
                or additional_params.get('benefit_plan_id')
            )
        if benefit_plan_id:
            benefit_plan_query = BenefitPlan.objects.filter(
                id=benefit_plan_id,
                is_deleted=False,
            )
        else:
            benefit_plan_query = BenefitPlan.objects.filter(is_deleted=False, beneficiary_data_schema__isnull=False)
            if additional_params and 'type' in additional_params:
                benefit_plan_query = benefit_plan_query.filter(type=additional_params['type'])
        list_of_tuple_with_definitions = self.__process_schema_and_build_tuple(benefit_plan_query, tuple_type)
        return list_of_tuple_with_definitions

    def apply_filter_to_queryset(self, custom_filters: List[namedtuple], query: QuerySet, relation=None) -> QuerySet:
        """
        Apply custom filters to a queryset.

        :param custom_filters: Structure of custom filter tuple: <Type>(field=<str>, filter=<str>, type=<str>).
        Example usage of filter tuple: BenefitPlan(field='income', filter='lt, gte, icontains, exact', type='integer')

        :param query: The original queryset with filters for example: Queryset[Beneficiary].

        :param relation: The optional argument which defines the related field in queryset for example 'beneficiary'
        :type relation: str or None

        :return: The updated queryset with additional filters applied for example: Queryset[Beneficiary].
        """
        grouped_by_field = {}
        for filter_part in custom_filters:
            try:
                json_field, value_type, raw_value = parse_custom_filter_part(filter_part)
            except (ValueError, KeyError, TypeError):
                logger.error(f"Invalid filter format: {filter_part}")
                continue

            value = self._resolve_filter_value(raw_value, value_type)
            if value == "" and isinstance(raw_value, dict):
                logger.warning(
                    "Location filter value dict has no name/code for field path %s: %s",
                    json_field,
                    raw_value,
                )
                continue

            if "__" in json_field:
                field_name, lookup = json_field.rsplit("__", 1)
            else:
                field_name, lookup = json_field, "exact"

            prefix = f"{relation}__json_ext__" if relation else "json_ext__"
            field_q = Q(**{f"{prefix}{field_name}__{lookup}": value})
            # json_ext location values may be stored as {name, code} objects
            field_q |= Q(**{f"{prefix}{field_name}__name__{lookup}": value})
            grouped_by_field.setdefault(field_name, []).append(field_q)

        for q_list in grouped_by_field.values():
            combined = q_list[0]
            for extra in q_list[1:]:
                combined |= extra
            query = query.filter(combined)
        return query.distinct()

    def suggest_filter_values(self, field: str, search: str, limit: int = 20, **kwargs) -> List[dict]:
        search = (search or "").strip()
        if not search:
            return []

        benefit_plan = self._resolve_benefit_plan_for_suggestions(**kwargs)
        if not benefit_plan:
            return []

        field_def = self._field_definition_from_schema(benefit_plan, field)
        if not field_def:
            return []

        field_type = field_def.get("type")
        has_location_fallback = bool(field_def.get("typeLocation") or field_def.get("referential"))

        if field_type == "boolean":
            return self._boolean_suggestions(search, limit)

        if field_type in ("string", "integer", "numeric", "number", "decimal"):
            # Mode A (prioritaire) : valeurs texte déjà présentes dans json_ext
            suggestions = self._distinct_json_ext_suggestions(
                benefit_plan, field, search, limit, relation="individual", **kwargs
            )
            if not suggestions:
                suggestions = self._distinct_json_ext_suggestions(
                    benefit_plan, field, search, limit, relation=None, **kwargs
                )
            if suggestions:
                return suggestions
            # Mode B : sélecteur location (typeLocation / referential) si aucune valeur texte
            if has_location_fallback:
                return []

        return []

    @staticmethod
    def _resolve_benefit_plan_for_suggestions(**kwargs) -> Optional[BenefitPlan]:
        benefit_plan_id = kwargs.get("uuid") or kwargs.get("benefit_plan_id")
        additional_params = kwargs.get("additional_params") or {}
        if not benefit_plan_id and isinstance(additional_params, dict):
            benefit_plan_id = additional_params.get("benefitPlan") or additional_params.get("benefit_plan_id")
        if not benefit_plan_id:
            return None
        return BenefitPlan.objects.filter(id=benefit_plan_id, is_deleted=False).first()

    @staticmethod
    def _field_definition_from_schema(benefit_plan: BenefitPlan, field: str) -> Optional[dict]:
        schema = benefit_plan.beneficiary_data_schema or {}
        properties = schema.get("properties") or {}
        field_def = properties.get(field)
        return field_def if isinstance(field_def, dict) else None

    @staticmethod
    def _boolean_suggestions(search: str, limit: int) -> List[dict]:
        options = [
            {"value": "true", "label": "true"},
            {"value": "false", "label": "false"},
        ]
        needle = search.lower()
        return [item for item in options if needle in item["label"]][:limit]

    @staticmethod
    def _resolve_json_ext_dict(raw: Any) -> dict:
        if not raw:
            return {}
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
                return parsed if isinstance(parsed, dict) else {}
            except (TypeError, ValueError):
                return {}
        return {}

    @staticmethod
    def _json_ext_field_path(field: str, relation: Optional[str] = None) -> str:
        if relation:
            return f"{relation}__json_ext__{field}"
        return f"json_ext__{field}"

    def _resolve_filter_value(self, raw_value, value_type: str):
        """
        Mode A : chaîne ou scalaire issu de json_ext.
        Mode B : objet location {name, code, ...} — on filtre sur le nom, puis le code.
        """
        if isinstance(raw_value, dict):
            for key in ("name", "code", "label"):
                candidate = raw_value.get(key)
                if candidate is not None and str(candidate).strip():
                    return str(candidate).strip()
            return ""
        return self.__cast_value(raw_value, value_type)

    @staticmethod
    def _distinct_json_ext_suggestions(
        benefit_plan: BenefitPlan,
        field: str,
        search: str,
        limit: int,
        relation: Optional[str] = None,
        **kwargs,
    ) -> List[dict]:
        field_path = BenefitPlanCustomFilterWizard._json_ext_field_path(field, relation)
        lookup = f"{field_path}__icontains"
        queryset = (
            Beneficiary.objects.filter(
                benefit_plan=benefit_plan,
                status=BeneficiaryStatus.ACTIVE,
                is_deleted=False,
            )
            .exclude(**{f"{field_path}__isnull": True})
            .exclude(**{field_path: ""})
            .filter(**{lookup: search})
            .values_list(field_path, flat=True)
            .distinct()[: limit * 3]
        )

        suggestions = []
        seen = set()
        needle = search.lower()
        for raw_value in queryset:
            label = BenefitPlanCustomFilterWizard._format_suggestion_label(raw_value)
            if not label:
                continue
            if needle not in label.lower():
                continue
            value = BenefitPlanCustomFilterWizard._format_suggestion_value(raw_value)
            key = (value, label)
            if key in seen:
                continue
            seen.add(key)
            suggestions.append({"value": value, "label": label})
            if len(suggestions) >= limit:
                break
        return suggestions

    @staticmethod
    def _format_suggestion_label(raw_value) -> str:
        if isinstance(raw_value, dict):
            for candidate_key in ("name", "code", "label", "uuid"):
                candidate = raw_value.get(candidate_key)
                if candidate:
                    return str(candidate)
            return str(raw_value)
        if raw_value is None:
            return ""
        return str(raw_value).strip()

    @staticmethod
    def _format_suggestion_value(raw_value) -> str:
        if isinstance(raw_value, dict):
            if raw_value.get("name") is not None:
                return str(raw_value["name"])
            if raw_value.get("code") is not None:
                return str(raw_value["code"])
            if raw_value.get("uuid") is not None:
                return str(raw_value["uuid"])
            return json.dumps(raw_value, ensure_ascii=False)
        if raw_value is None:
            return ""
        return str(raw_value).strip()

    def __process_schema_and_build_tuple(
            self,
            benefit_plan_query: QuerySet[BenefitPlan],
            tuple_type: type
    ) -> List[namedtuple]:
        tuples_with_definitions = []
        existing_keys = set()

        for benefit_plan in benefit_plan_query:
            schema = benefit_plan.beneficiary_data_schema
            if schema and 'properties' in schema:
                properties = schema['properties']
                for key, value in properties.items():
                    if key in existing_keys or not isinstance(value, dict):
                        continue
                    field_type = value.get('type')
                    if field_type not in self.FILTERS_BASED_ON_FIELD_TYPE:
                        logger.warning(
                            'Skipping unsupported custom filter field %s (type=%s) in benefit plan schema',
                            key,
                            field_type,
                        )
                        continue
                    tuple_with_definition = tuple_type(
                        field=key,
                        filter=self.FILTERS_BASED_ON_FIELD_TYPE[field_type],
                        type=field_type,
                        referential=value.get('referential'),
                        typeLocation=value.get('typeLocation'),
                    )
                    tuples_with_definitions.append(tuple_with_definition)
                    existing_keys.add(key)

            else:
                logger.warning('Cannot retrieve definitions of filters based '
                               'on the provided schema due to either empty schema '
                               'or missing properties in schema file')

        return tuples_with_definitions

    def __cast_value(self, value: str, value_type: str):
        if value_type == 'integer':
            return int(value)
        elif value_type == 'numeric':
            return float(value)
        elif value_type == 'boolean':
            cleaned_value = self.__remove_unexpected_chars(value)
            return cleaned_value.lower() == 'true'
        elif value_type == 'date':
            # parser avec datetime.strptime si besoin
            return value
        elif value_type == 'string':
            # Si c'est un JSON -> essayer de parser
            try:
                obj = json.loads(value)
                if isinstance(obj, dict):
                    return obj.get("name", "")  # récupère uniquement le "name"
                return str(obj)
            except Exception as e:
                return str(value).strip('"')

        return value

    def __remove_unexpected_chars(self, string: str):
        pattern = r'[^\w\s]'  # Remove any character that is not alphanumeric or whitespace

        # Use re.sub() to remove the unwanted characters
        cleaned_string = re.sub(pattern, '', string)

        return cleaned_string
