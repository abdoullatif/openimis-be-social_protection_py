from django.apps import AppConfig

from core.custom_filters import CustomFilterRegistryPoint
from core.data_masking import MaskingClassRegistryPoint


DEFAULT_CONFIG = {
    "gql_benefit_plan_search_perms": ["160001"],
    "gql_benefit_plan_create_perms": ["160002"],
    "gql_benefit_plan_update_perms": ["160003"],
    "gql_benefit_plan_delete_perms": ["160004"],
    "gql_benefit_plan_close_perms": ["160005"],
    "gql_beneficiary_search_perms": ["170001"],
    "gql_beneficiary_create_perms": ["170002"],
    "gql_beneficiary_update_perms": ["170003"],
    "gql_beneficiary_delete_perms": ["170004"],
    "gql_schema_search_perms": ["171001"],
    "gql_schema_create_perms": ["171002"],
    "gql_schema_update_perms": ["171003"],
    "gql_schema_delete_perms": ["171004"],


    # Create task for model instead of performing crud action
    "gql_check_benefit_plan_update": True,
    "gql_check_beneficiary_crud": True,
    "gql_check_group_beneficiary_crud": True,
    "unique_class_validation": "DeduplicationValidationStrategy",
    "validation_calculation_uuid": "4362f958-5894-435b-9bda-df6cadf88352",
    "enable_maker_checker_for_beneficiary_upload": True,
    "enable_maker_checker_for_beneficiary_update": True,
    "validation_import_valid_items": "validation.import_valid_items",
    "validation_import_valid_items": "validation.import_valid_items",
    "validation_import_group_valid_items": "validation.import_group_valid_items",
    "validation_upload_valid_items": "validation.upload_valid_items",
    "validation_download_invalid_items": "validation.download_invalid_items",
    "benefit_plan_suspend": "benefit_plan.benefit_plan_suspend",

    "validation_import_valid_items_workflow": "beneficiary-import-valid-items.beneficiary-import-valid-items",
    "validation_upload_valid_items_workflow": "beneficiary-upload-valid-items.beneficiary-upload-valid-items",
    "validation_enrollment": "validation-enrollment",
    "validation_group_enrollment": "validation-group-enrollment",

    "enable_maker_checker_logic_enrollment": True,
    "enable_maker_checker_for_group_upload": True,
    "beneficiary_mask_fields": [
        'json_ext.beneficiary_data_source',
        'json_ext.educated_level'
    ],
    "group_beneficiary_mask_fields": [
        'json_ext.beneficiary_data_source',
        'json_ext.educated_level'
    ],
    "beneficiary_base_fields": [
        'first_name', 'last_name', 'dob', 'location_name', 'location_code', 'id'
    ],
    "social_protection_masking_enabled": True,
    "enable_python_workflows": True,
    "default_beneficiary_status": "POTENTIAL",

    # Champs autorisés dans le payload sync opérateur (liste vide = tous)
    "operator_sync_fields": [],
}


class SocialProtectionConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'social_protection'

    gql_benefit_plan_search_perms = None
    gql_benefit_plan_create_perms = None
    gql_benefit_plan_update_perms = None
    gql_benefit_plan_delete_perms = None
    gql_benefit_plan_close_perms = None
    gql_beneficiary_search_perms = None
    gql_beneficiary_create_perms = None
    gql_beneficiary_update_perms = None
    gql_beneficiary_delete_perms = None
    gql_schema_search_perms = None
    gql_schema_create_perms = None
    gql_schema_update_perms = None
    gql_schema_delete_perms = None

    gql_check_benefit_plan_update = None
    gql_check_beneficiary_crud = None
    gql_check_group_beneficiary_crud = None
    unique_class_validation = None
    validation_calculation_uuid = None
    validation_import_valid_items = None
    validation_upload_valid_items = None
    validation_download_invalid_items = None
    validation_import_valid_items_workflow = None
    validation_upload_valid_items_workflow = None
    validation_enrollment = None
    validation_group_enrollment = None
    validation_import_group_valid_items = None
    benefit_plan_suspend = None

    enable_maker_checker_for_beneficiary_upload = None
    enable_maker_checker_for_beneficiary_update = None

    enable_python_workflows = None
    enable_maker_checker_logic_enrollment = None
    enable_maker_checker_for_group_upload = None
    beneficiary_mask_fields = None
    group_beneficiary_mask_fields = None
    beneficiary_base_fields = None
    social_protection_masking_enabled = None

    default_beneficiary_status = None

    operator_sync_fields = None

    def ready(self):
        from core.models import ModuleConfiguration

        cfg = ModuleConfiguration.get_or_default(self.name, DEFAULT_CONFIG)
        self.__load_config(cfg)
        self._set_up_workflows()
        self.__register_masking_class()

    def _set_up_workflows(self):
        from workflow.systems.python import PythonWorkflowAdaptor
        from social_protection.workflows import process_import_beneficiaries_workflow, \
            process_update_beneficiaries_workflow, \
            process_import_valid_beneficiaries_workflow, \
            process_update_valid_beneficiaries_workflow

        if self.enable_python_workflows:
            PythonWorkflowAdaptor.register_workflow(
                'Python Beneficiaries Upload',
                'socialProtection',
                process_import_beneficiaries_workflow
            )
            PythonWorkflowAdaptor.register_workflow(
                'Python Beneficiaries Update',
                'socialProtection',
                process_update_beneficiaries_workflow
            )
            PythonWorkflowAdaptor.register_workflow(
                'Python Beneficiaries Valid Upload',
                'socialProtection',
                process_import_valid_beneficiaries_workflow
            )
            PythonWorkflowAdaptor.register_workflow(
                'Python Beneficiaries Valid Update',
                'socialProtection',
                process_update_valid_beneficiaries_workflow
            )

        # Replace default setup for invalid workflow to be python one
        if SocialProtectionConfig.enable_python_workflows is True:

            # Resolve Maker-Checker Workflows Overwrite
            if self.validation_import_valid_items_workflow == DEFAULT_CONFIG['validation_import_valid_items_workflow']:
                SocialProtectionConfig.validation_import_valid_items_workflow \
                    = 'socialProtection.Python Beneficiaries Valid Upload'

            if self.validation_upload_valid_items_workflow == DEFAULT_CONFIG['validation_upload_valid_items_workflow']:
                SocialProtectionConfig.validation_upload_valid_items_workflow \
                    = 'socialProtection.Python Beneficiaries Valid Update'

            # # Create Maker-Checker Logic tasks
            # if self.validation_import_valid_items == DEFAULT_CONFIG['validation_import_valid_items']:
            #     SocialProtectionConfig.validation_import_valid_items \
            #         = 'socialProtection.Python Beneficiaries Valid Upload'
            # if self.validation_upload_valid_items == DEFAULT_CONFIG['validation_upload_valid_items']:
            #     SocialProtectionConfig.validation_upload_valid_items \
            #         = 'socialProtection.Python Beneficiaries Valid Update'

    @classmethod
    def __load_config(cls, cfg):
        """
        Load all config fields that match current AppConfig class fields, all custom fields have to be loaded separately
        """
        for field in cfg:
            if hasattr(SocialProtectionConfig, field):
                setattr(SocialProtectionConfig, field, cfg[field])

        from social_protection.custom_filters import BenefitPlanCustomFilterWizard
        CustomFilterRegistryPoint.register_custom_filters(
            module_name=cls.name,
            custom_filter_class_list=[BenefitPlanCustomFilterWizard]
        )

    def __register_masking_class(cls):
        from social_protection.data_masking import (
            BeneficiaryMask,
            GroupBeneficiaryMask
        )
        MaskingClassRegistryPoint.register_masking_class(
            masking_class_list=[BeneficiaryMask(), GroupBeneficiaryMask()]
        )

    @staticmethod
    def get_beneficiary_upload_file_path(benefit_plan_id, file_name=None):
        if file_name:
            return f"beneficiary_upload/benefit_plan_{benefit_plan_id}/{file_name}"
        return f"beneficiary_upload/benefit_plan_{benefit_plan_id}"
