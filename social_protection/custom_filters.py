import logging
import re

from collections import namedtuple
from django.db.models.query import QuerySet
from typing import List

from core.custom_filters import CustomFilterWizardInterface
from social_protection.models import BenefitPlan


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
        if benefit_plan_id:
            benefit_plan_query = BenefitPlan.objects.filter(id=benefit_plan_id)
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
        for filter_part in custom_filters:
            if isinstance(filter_part, dict):
                value_type = filter_part['type']
                value = filter_part['value']
                field = filter_part['field'] + '__' + filter_part['filter']
            else:
                field, value = filter_part.split('=')
                field, value_type = field.rsplit('__', 1)
            value = self.__cast_value(value, value_type)
            filter_kwargs = {f"{relation}__json_ext__{field}" if relation else f"json_ext__{field}": value}
            query = query.filter(**filter_kwargs).distinct()
        return query

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
                    if key not in existing_keys:
                        tuple_with_definition = tuple_type(
                            field=key,
                            filter=self.FILTERS_BASED_ON_FIELD_TYPE[value['type']],
                            type=value['type'],
                            referential=value['referential'] if 'referential' in value else None,
                            typeLocation=value['typeLocation'] if 'typeLocation' in value else None
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
        elif value_type == 'string':
            return str(value[1:-1])
        elif value_type == 'numeric':
            return float(value)
        elif value_type == 'boolean':
            cleaned_value = self.__remove_unexpected_chars(value)
            if cleaned_value.lower() == 'true':
                return True
            elif cleaned_value.lower() == 'false':
                return False
        elif value_type == 'date':
            # Perform date parsing logic here
            # Assuming you have a specific date format, you can use datetime.strptime
            # Example: return datetime.strptime(value, '%Y-%m-%d').date()
            pass

        # Return None if the value type is not recognized
        return None

    def __remove_unexpected_chars(self, string: str):
        pattern = r'[^\w\s]'  # Remove any character that is not alphanumeric or whitespace

        # Use re.sub() to remove the unwanted characters
        cleaned_string = re.sub(pattern, '', string)

        return cleaned_string
