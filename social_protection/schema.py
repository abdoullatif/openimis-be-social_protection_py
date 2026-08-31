import graphene
import pandas as pd

from django.contrib.auth.models import AnonymousUser
from django.db.models import Q, Case, When, BooleanField, Value
from django.core.exceptions import PermissionDenied

from django.utils.translation import gettext as _
from core.custom_filters import CustomFilterWizardStorage
from core.gql_queries import ValidationMessageGQLType
from core.schema import OrderedDjangoFilterConnectionField
from core.services import wait_for_mutation
from core.utils import append_validity_filter, validate_json_schema
from social_protection.apps import SocialProtectionConfig
from social_protection.gql_mutations import (
    CreateBenefitPlanMutation,
    UpdateBenefitPlanMutation,
    DeleteBenefitPlanMutation,
    CloseBenefitPlanMutation,
    CreateBeneficiaryMutation,
    UpdateBeneficiaryMutation,
    DeleteBeneficiaryMutation, CreateGroupBeneficiaryMutation, UpdateGroupBeneficiaryMutation,
    DeleteGroupBeneficiaryMutation
)
from social_protection.gql_queries import (
    BenefitPlanGQLType,
    BeneficiaryGQLType, GroupBeneficiaryGQLType,
    BenefitPlanDataUploadQGLType, BenefitPlanSchemaFieldsGQLType,
    BenefitPlanHistoryGQLType, BeneficiaryChangeLogGQLType
)
from social_protection.export_mixin import ExportableSocialProtectionQueryMixin
from social_protection.models import (
    BenefitPlan,
    Beneficiary, GroupBeneficiary, BenefitPlanDataUploadRecords, BeneficiaryChangeLog
)
from social_protection.validation import validate_bf_unique_code, validate_bf_unique_name
import graphene_django_optimizer as gql_optimizer
from location.apps import LocationConfig


def patch_details(beneficiary_df: pd.DataFrame):
    # Transform extension to DF columns 
    df_unfolded = pd.json_normalize(beneficiary_df['json_ext'])
    # Merge unfolded DataFrame with the original DataFrame
    df_final = pd.concat([beneficiary_df, df_unfolded], axis=1)
    df_final = df_final.drop('json_ext', axis=1)
    return df_final


class BfTypeEnum(graphene.Enum):
    INDIVIDUAL = BenefitPlan.BenefitPlanType.INDIVIDUAL_TYPE
    GROUP = BenefitPlan.BenefitPlanType.GROUP_TYPE


class Query(ExportableSocialProtectionQueryMixin, graphene.ObjectType):
    export_patches = {
        'beneficiary': [
            patch_details
        ],
        'group_beneficiary': [
            patch_details
        ]
    }
    exportable_fields = ['beneficiary', 'group_beneficiary']
    module_name = "social_protection"
    object_type = "BenefitPlan"

    benefit_plan = OrderedDjangoFilterConnectionField(
        BenefitPlanGQLType,
        orderBy=graphene.List(of_type=graphene.String),
        dateValidFrom__Gte=graphene.DateTime(),
        dateValidTo__Lte=graphene.DateTime(),
        applyDefaultValidityFilter=graphene.Boolean(),
        client_mutation_id=graphene.String(),
        individual_id=graphene.String(),
        group_id=graphene.String(),
        beneficiary_status=graphene.String(),
        search=graphene.String(),
        sort_alphabetically=graphene.Boolean(),
    )
    beneficiary = OrderedDjangoFilterConnectionField(
        BeneficiaryGQLType,
        orderBy=graphene.List(of_type=graphene.String),
        dateValidFrom__Gte=graphene.DateTime(),
        dateValidTo__Lte=graphene.DateTime(),
        parent_location=graphene.String(),
        parent_location_level=graphene.Int(),
        applyDefaultValidityFilter=graphene.Boolean(),
        client_mutation_id=graphene.String(),
        customFilters=graphene.List(of_type=graphene.String),
    )
    group_beneficiary = OrderedDjangoFilterConnectionField(
        GroupBeneficiaryGQLType,
        orderBy=graphene.List(of_type=graphene.String),
        dateValidFrom__Gte=graphene.DateTime(),
        dateValidTo__Lte=graphene.DateTime(),
        parent_location=graphene.String(),
        parent_location_level=graphene.Int(),
        applyDefaultValidityFilter=graphene.Boolean(),
        client_mutation_id=graphene.String(),
        customFilters=graphene.List(of_type=graphene.String),
    )

    beneficiary_data_upload_history = OrderedDjangoFilterConnectionField(
        BenefitPlanDataUploadQGLType,
        orderBy=graphene.List(of_type=graphene.String),
        dateValidFrom__Gte=graphene.DateTime(),
        dateValidTo__Lte=graphene.DateTime(),
        applyDefaultValidityFilter=graphene.Boolean(),
        client_mutation_id=graphene.String()
    )

    beneficiary_change_log = OrderedDjangoFilterConnectionField(
        BeneficiaryChangeLogGQLType,
        orderBy=graphene.List(of_type=graphene.String),
        dateValidFrom__Gte=graphene.DateTime(),
        dateValidTo__Lte=graphene.DateTime(),
        applyDefaultValidityFilter=graphene.Boolean(),
    )

    bf_code_validity = graphene.Field(
        ValidationMessageGQLType,
        bf_code=graphene.String(required=True),
        description="Checks that the specified Benefit Plan code is valid"
    )
    bf_name_validity = graphene.Field(
        ValidationMessageGQLType,
        bf_name=graphene.String(required=True),
        description="Checks that the specified Benefit Plan name is valid"
    )
    bf_schema_validity = graphene.Field(
        ValidationMessageGQLType,
        bf_schema=graphene.String(required=True),
        description="Checks that the specified Benefit Plan schema is valid"
    )
    benefit_plan_schema_field = graphene.Field(
        BenefitPlanSchemaFieldsGQLType,
        bf_type=graphene.Argument(BfTypeEnum),
        description="Endpoint responsible for getting all fields from all BF schemas"
    )
    benefit_plan_history = OrderedDjangoFilterConnectionField(
        BenefitPlanHistoryGQLType,
        orderBy=graphene.List(of_type=graphene.String),
        dateValidFrom__Gte=graphene.DateTime(),
        dateValidTo__Lte=graphene.DateTime(),
        applyDefaultValidityFilter=graphene.Boolean(),
        client_mutation_id=graphene.String(),
        individual_id=graphene.String(),
        group_id=graphene.String(),
        beneficiary_status=graphene.String(),
        search=graphene.String(),
        sort_alphabetically=graphene.Boolean(),
    )

    def resolve_bf_code_validity(self, info, **kwargs):
        if not info.context.user.has_perms(SocialProtectionConfig.gql_benefit_plan_search_perms):
            raise PermissionDenied(_("unauthorized"))
        errors = validate_bf_unique_code(kwargs['bf_code'])
        if errors:
            return ValidationMessageGQLType(False, error_message=errors[0]['message'])
        else:
            return ValidationMessageGQLType(True)

    def resolve_bf_name_validity(self, info, **kwargs):
        if not info.context.user.has_perms(SocialProtectionConfig.gql_benefit_plan_search_perms):
            raise PermissionDenied(_("unauthorized"))
        errors = validate_bf_unique_name(kwargs['bf_name'])
        if errors:
            return ValidationMessageGQLType(False, error_message=errors[0]['message'])
        else:
            return ValidationMessageGQLType(True)

    def resolve_bf_schema_validity(self, info, **kwargs):
        if not info.context.user.has_perms(SocialProtectionConfig.gql_benefit_plan_search_perms):
            raise PermissionDenied(_("unauthorized"))
        errors = validate_json_schema(kwargs['bf_schema'])
        if errors:
            return ValidationMessageGQLType(False, error_message=errors[0]['message'])
        else:
            return ValidationMessageGQLType(True)

    def resolve_benefit_plan(self, info, **kwargs):
        filters = append_validity_filter(**kwargs)

        search = kwargs.get("search", None)
        if search:
            search_terms = search.split(' ')
            search_queries = Q()
            for term in search_terms:
                search_queries |= Q(code__icontains=term) | Q(name__icontains=term)
            filters.append(search_queries)

        client_mutation_id = kwargs.get("client_mutation_id", None)
        if client_mutation_id:
            wait_for_mutation(client_mutation_id)
            filters.append(Q(mutations__mutation__client_mutation_id=client_mutation_id))

        individual_id = kwargs.get("individual_id", None)
        if individual_id:
            filters.append(Q(
                Q(beneficiary__individual__id=individual_id) |
                Q(groupbeneficiary__group__groupindividuals__individual__id=individual_id)
            ))

        group_id = kwargs.get("group_id", None)
        if group_id:
            filters.append(Q(groupbeneficiary__group__id=group_id))

        beneficiary_status = kwargs.get("beneficiary_status", None)
        if beneficiary_status:
            filters.append(Q(beneficiary__status=beneficiary_status) | Q(groupbeneficiary__status=beneficiary_status))

        Query._check_permissions(
            info.context.user,
            SocialProtectionConfig.gql_benefit_plan_search_perms
        )

        query = BenefitPlan.objects.filter(*filters)

        sort_alphabetically = kwargs.get("sort_alphabetically", None)
        if sort_alphabetically:
            query = query.order_by('code')
        return gql_optimizer.query(query, info)

    def resolve_beneficiary(self, info, **kwargs):
        def _build_filters(info, **kwargs):
            filters = append_validity_filter(**kwargs)

            client_mutation_id = kwargs.get("client_mutation_id")
            if client_mutation_id:
                filters.append(Q(mutations__mutation__client_mutation_id=client_mutation_id))

            Query._check_permissions(
                info.context.user,
                SocialProtectionConfig.gql_beneficiary_search_perms
            )
            return filters

        def _apply_custom_filters(query, **kwargs):
            custom_filters = kwargs.get("customFilters")
            if custom_filters:
                query = CustomFilterWizardStorage.build_custom_filters_queryset(
                    Query.module_name,
                    Query.object_type,
                    custom_filters,
                    query
                )
            return query

        def _get_eligible_uuids(query, info, **kwargs):
            status = kwargs.get("status")
            benefit_plan_id = kwargs.get("benefit_plan__id")
            default_results = (set(), False)  # No eligibility check was performed

            if not status or not benefit_plan_id:
                return default_results

            benefit_plan = BenefitPlan.objects.filter(id=benefit_plan_id).first()
            if not benefit_plan:
                return default_results

            eligibility_filters = (benefit_plan.json_ext or {}).get('advanced_criteria', {}).get(status)
            if not eligibility_filters:
                return default_results

            query_eligible = CustomFilterWizardStorage.build_custom_filters_queryset(
                Query.module_name,
                Query.object_type,
                eligibility_filters,
                query
            )
            eligible_beneficiaries = gql_optimizer.query(query_eligible, info)
            eligible_uuids = set(eligible_beneficiaries.values_list('uuid', flat=True))
            return eligible_uuids, True  # Eligibility check was performed

        def _annotate_is_eligible(query, eligible_uuids, eligibility_check_performed):
            return query.annotate(
                is_eligible=Case(
                    When(uuid__in=eligible_uuids, then=Value(True)),
                    When(~Q(uuid__in=eligible_uuids) & Value(eligibility_check_performed), then=Value(False)),
                    default=Value(None),
                    output_field=BooleanField()
                )
            )

        filters = _build_filters(info, **kwargs)
        
        parent_location = kwargs.get('parent_location')
        parent_location_level = kwargs.get('parent_location_level')
        if parent_location is not None and parent_location_level is not None:
            filters.append(Query._get_location_filters(parent_location, parent_location_level, prefix='individual__'))

        query = Beneficiary.get_queryset(None, info.context.user)
        query = _apply_custom_filters(query.filter(*filters), **kwargs)

        eligible_uuids, eligibility_check_performed = _get_eligible_uuids(query, info, **kwargs)
        query = _annotate_is_eligible(query, eligible_uuids, eligibility_check_performed)

        return gql_optimizer.query(query, info)

    def resolve_group_beneficiary(self, info, **kwargs):
        def _build_filters(info, **kwargs):
            filters = append_validity_filter(**kwargs)

            client_mutation_id = kwargs.get("client_mutation_id")
            if client_mutation_id:
                filters.append(Q(mutations__mutation__client_mutation_id=client_mutation_id))

            Query._check_permissions(
                info.context.user,
                SocialProtectionConfig.gql_beneficiary_search_perms
            )
            return filters

        def _apply_custom_filters(query, **kwargs):
            custom_filters = kwargs.get("customFilters")
            if custom_filters:
                query = CustomFilterWizardStorage.build_custom_filters_queryset(
                    Query.module_name,
                    Query.object_type,
                    custom_filters,
                    query,
                    "group__groupindividuals__individual",
                )
            return query

        def _get_eligible_group_uuids(query, info, **kwargs):
            status = kwargs.get("status")
            benefit_plan_id = kwargs.get("benefit_plan__id")
            default_results = (set(), False)  # No eligibility check was performed

            if not status or not benefit_plan_id:
                return default_results

            benefit_plan = BenefitPlan.objects.filter(id=benefit_plan_id).first()
            if not benefit_plan:
                return default_results

            eligibility_filters = (benefit_plan.json_ext or {}).get('advanced_criteria', {}).get(status)
            if not eligibility_filters:
                return default_results

            query_eligible = CustomFilterWizardStorage.build_custom_filters_queryset(
                Query.module_name,
                Query.object_type,
                eligibility_filters,
                query,
                "group__groupindividuals__individual",
            )
            eligible_group_beneficiaries = gql_optimizer.query(query_eligible, info)
            eligible_group_uuids = set(eligible_group_beneficiaries.values_list('uuid', flat=True))
            return eligible_group_uuids, True  # Eligibility check was performed

        def _annotate_is_eligible(query, eligible_group_uuids, eligibility_check_performed):
            return query.annotate(
                is_eligible=Case(
                    When(uuid__in=eligible_group_uuids, then=Value(True)),
                    When(~Q(uuid__in=eligible_group_uuids) & Value(eligibility_check_performed), then=Value(False)),
                    default=Value(None),
                    output_field=BooleanField()
                )
            )

        filters = _build_filters(info, **kwargs)
        
        parent_location = kwargs.get('parent_location')
        parent_location_level = kwargs.get('parent_location_level')
        if parent_location is not None and parent_location_level is not None:
            filters.append(Query._get_location_filters(parent_location, parent_location_level, prefix='group__'))

        query = GroupBeneficiary.get_queryset(None, info.context.user)
        query = _apply_custom_filters(query.filter(*filters), **kwargs)

        eligible_group_uuids, eligibility_check_performed = _get_eligible_group_uuids(query, info, **kwargs)
        query = _annotate_is_eligible(query, eligible_group_uuids, eligibility_check_performed)

        return gql_optimizer.query(query, info)


    def resolve_awaiting_beneficiary(self, info, **kwargs):
        filters = append_validity_filter(**kwargs)

        client_mutation_id = kwargs.get("client_mutation_id", None)
        if client_mutation_id:
            wait_for_mutation(client_mutation_id)
            filters.append(Q(mutations__mutation__client_mutation_id=client_mutation_id))

        Query._check_permissions(
            info.context.user,
            SocialProtectionConfig.gql_beneficiary_search_perms
        )
        query = GroupBeneficiary.objects.filter(*filters)
        return gql_optimizer.query(query, info)

    def resolve_beneficiary_data_upload_history(self, info, **kwargs):
        filters = append_validity_filter(**kwargs)

        client_mutation_id = kwargs.get("client_mutation_id", None)
        if client_mutation_id:
            wait_for_mutation(client_mutation_id)
            filters.append(Q(mutations__mutation__client_mutation_id=client_mutation_id))

        Query._check_permissions(
            info.context.user,
            SocialProtectionConfig.gql_beneficiary_search_perms
        )
        query = BenefitPlanDataUploadRecords.objects.filter(*filters)
        return gql_optimizer.query(query, info)

    def resolve_beneficiary_change_log(self, info, **kwargs):
        filters = append_validity_filter(**kwargs)
        Query._check_permissions(
            info.context.user,
            SocialProtectionConfig.gql_beneficiary_search_perms
        )
        query = BeneficiaryChangeLog.objects.filter(*filters)
        return gql_optimizer.query(query, info)

    def resolve_benefit_plan_schema_field(self, info, **kwargs):
        filters = append_validity_filter(**kwargs)

        Query._check_permissions(
            info.context.user,
            SocialProtectionConfig.gql_schema_search_perms
        )

        bf_type = kwargs.get("bf_type", None)
        if bf_type:
            filters.append(Q(type=bf_type))

        query = BenefitPlan.objects.filter(*filters)
        return gql_optimizer.query(query, info)

    @staticmethod
    def _check_permissions(user, permission):
        if type(user) is AnonymousUser or not user.id or not user.has_perms(permission):
            raise PermissionError("Unauthorized")

    def resolve_benefit_plan_history(self, info, **kwargs):
        filters = append_validity_filter(**kwargs)

        search = kwargs.get("search", None)
        if search:
            search_terms = search.split(' ')
            search_queries = Q()
            for term in search_terms:
                search_queries |= Q(code__icontains=term) | Q(name__icontains=term)
            filters.append(search_queries)

        client_mutation_id = kwargs.get("client_mutation_id", None)
        if client_mutation_id:
            wait_for_mutation(client_mutation_id)
            filters.append(Q(mutations__mutation__client_mutation_id=client_mutation_id))

        individual_id = kwargs.get("individual_id", None)
        if individual_id:
            filters.append(Q(beneficiary__individual__id=individual_id))

        group_id = kwargs.get("group_id", None)
        if group_id:
            filters.append(Q(groupbeneficiary__group__id=group_id))

        beneficiary_status = kwargs.get("beneficiary_status", None)
        if beneficiary_status:
            filters.append(Q(beneficiary__status=beneficiary_status) | Q(groupbeneficiary__status=beneficiary_status))

        Query._check_permissions(
            info.context.user,
            SocialProtectionConfig.gql_benefit_plan_search_perms
        )

        query = BenefitPlan.history.filter(*filters)

        sort_alphabetically = kwargs.get("sort_alphabetically", None)
        if sort_alphabetically:
            query = query.order_by('code')
        return gql_optimizer.query(query, info)
    
    @staticmethod
    def _get_location_filters(parent_location, parent_location_level, prefix=""):
        query_key = "uuid"
        for i in range(len(LocationConfig.location_types) - parent_location_level - 1):
            query_key = "parent__" + query_key
        query_key = prefix + "location__" + query_key
        return Q(**{query_key: parent_location})


class Mutation(graphene.ObjectType):
    create_benefit_plan = CreateBenefitPlanMutation.Field()
    update_benefit_plan = UpdateBenefitPlanMutation.Field()
    delete_benefit_plan = DeleteBenefitPlanMutation.Field()
    close_benefit_plan = CloseBenefitPlanMutation.Field()

    create_beneficiary = CreateBeneficiaryMutation.Field()
    update_beneficiary = UpdateBeneficiaryMutation.Field()
    delete_beneficiary = DeleteBeneficiaryMutation.Field()

    create_group_beneficiary = CreateGroupBeneficiaryMutation.Field()
    update_group_beneficiary = UpdateGroupBeneficiaryMutation.Field()
    delete_group_beneficiary = DeleteGroupBeneficiaryMutation.Field()
