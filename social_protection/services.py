import copy
import json
import logging
import uuid

import concurrent.futures
import math
import pandas as pd
from django.core.files.uploadedfile import InMemoryUploadedFile
from django.db import transaction
from django.db import models
from django.db.models import Q, Value, Func, F
from django.db.models.functions import Concat
from pandas import DataFrame

from calculation.services import get_calculation_object
from core.services import BaseService
from core.signals import register_service_signal
from individual.models import IndividualDataSourceUpload, IndividualDataSource, Individual
from social_protection.apps import SocialProtectionConfig
from social_protection.models import (
    BenefitPlan,
    Beneficiary,
    BenefitPlanDataUploadRecords,
    GroupBeneficiary,
    BeneficiaryStatus,
)

from social_protection.utils import load_dataframe, fetch_summary_of_valid_items, fetch_summary_of_broken_items
from social_protection.validation import (
    BeneficiaryValidation,
    BenefitPlanValidation, GroupBeneficiaryValidation
)
from tasks_management.services import UpdateCheckerLogicServiceMixin, CheckerLogicServiceMixin, \
    crud_business_data_builder
from workflow.systems.base import WorkflowHandler
from workflow.util import result as WorkflowExecutionResult
from core.models import User
from core.services.utils import check_authentication as check_authentication, output_exception, \
    model_representation, output_result_success

from social_protection.apps import SocialProtectionConfig

logger = logging.getLogger(__name__)


class BenefitPlanService(BaseService, UpdateCheckerLogicServiceMixin):
    OBJECT_TYPE = BenefitPlan

    def __init__(self, user, validation_class=BenefitPlanValidation):
        super().__init__(user, validation_class)

    @register_service_signal('benefit_plan_service.create')
    def create(self, obj_data):
        return super().create(obj_data)

    @register_service_signal('benefit_plan_service.update')
    def update(self, obj_data):
        return super().update(obj_data)

    @register_service_signal('benefit_plan_service.delete')
    def delete(self, obj_data):
        return super().delete(obj_data)

    @register_service_signal('benefit_plan_service.close')
    def close_benefit_plan(self, obj_data):
        from tasks_management.models import Task
        from tasks_management.apps import TasksManagementConfig
        from tasks_management.services import _get_std_task_data_payload, TaskService
        from social_protection.apps import SocialProtectionConfig
        benefit_plan = BenefitPlan.objects.filter(id=obj_data.get('id')).first()
        data = {'benefit_plan_id': benefit_plan.id}
        TaskService(self.user).create({
            'source': 'BenefitPlanService',
            'entity': benefit_plan,
            'status': Task.Status.RECEIVED,
            'executor_action_event': TasksManagementConfig.default_executor_event,
            'business_event': SocialProtectionConfig.benefit_plan_suspend,
            'data': _get_std_task_data_payload(data)
        })


class BeneficiaryService(BaseService, CheckerLogicServiceMixin):
    OBJECT_TYPE = Beneficiary

    def __init__(self, user, validation_class=BeneficiaryValidation):
        super().__init__(user, validation_class)

    def would_exceed_max_active_beneficiaries(self, benefit_plan_id, status, id=None):
        benefit_plan = BenefitPlan.objects.get(id=benefit_plan_id)
        if benefit_plan and status == "ACTIVE":
            max_active_beneficiaries = benefit_plan.max_beneficiaries
            active_beneficiaries = Beneficiary.objects.filter(is_deleted=False, benefit_plan_id=benefit_plan_id, status="ACTIVE").distinct()
            beneficiary_already_active = active_beneficiaries.filter(id=id).exists() if id else False
            return active_beneficiaries.count() == max_active_beneficiaries and not beneficiary_already_active
        return False

    @register_service_signal('beneficiary_service.create')
    def create(self, obj_data):
        try:
            status = obj_data.get("status", None)
            benefit_plan_id = obj_data.get("benefit_plan_id", None)

            if self.would_exceed_max_active_beneficiaries(benefit_plan_id, status):
                raise ValueError(f"Error creating beneficiary with active status. Benefit plan is already at max active beneficiaries")
            return super().create(obj_data)
        except Exception as exc:
            return output_exception(model_name=self.OBJECT_TYPE.__name__, method="update", exception=exc)
    
    @register_service_signal('beneficiary_service.update')
    def update(self, obj_data):
        try:
            status = obj_data.get("status", None)
            benefit_plan_id = obj_data.get("benefit_plan_id", None)
            id = obj_data.get('id', None)

            if self.would_exceed_max_active_beneficiaries(benefit_plan_id, status, id):
                raise ValueError(f"Error changing beneficiary to active status. Benefit plan is already at max active beneficiaries")
            return super().update(obj_data)
        except Exception as exc:
            return output_exception(model_name=self.OBJECT_TYPE.__name__, method="update", exception=exc)

    @register_service_signal('beneficiary_service.delete')
    def delete(self, obj_data):
        beneficiary_id = obj_data.get('id')
        beneficiary = Beneficiary.objects.filter(id=beneficiary_id).first()
        result = super().delete(obj_data)
        if beneficiary:
            try:
                from social_protection.operator_sync import capture_delete_from_beneficiary
                # Recharger au cas où le soft-delete a modifié l'instance
                beneficiary.refresh_from_db()
                capture_delete_from_beneficiary(self.user, beneficiary)
            except Exception:
                logger.exception(
                    "Failed to capture DELETE change log for beneficiary=%s", beneficiary_id
                )
        return result

    def _business_data_serializer(self, data):
        def serialize(key, value):
            if key == 'id':
                beneficiary = Beneficiary.objects.get(id=value)
                individual = beneficiary.individual
                return f'{individual.first_name} {individual.last_name}'
            elif key == 'benefit_plan_id':
                benefit_plan = BenefitPlan.objects.get(id=value)
                return benefit_plan.__str__()
            elif key == 'individual_id':
                individual = Individual.objects.get(id=value)
                return f'{individual.first_name} {individual.last_name}'
            else:
                return value

        serialized_data = crud_business_data_builder(data, serialize)
        return serialized_data


class GroupBeneficiaryService(BaseService, CheckerLogicServiceMixin):
    OBJECT_TYPE = GroupBeneficiary

    def __init__(self, user, validation_class=GroupBeneficiaryValidation):
        super().__init__(user, validation_class)

    def would_exceed_max_active_beneficiaries(self, benefit_plan_id, status, id=None):
        benefit_plan = BenefitPlan.objects.get(id=benefit_plan_id)
        if benefit_plan and status == "ACTIVE":
            max_active_beneficiaries = benefit_plan.max_beneficiaries
            active_beneficiaries = GroupBeneficiary.objects.filter(is_deleted=False, benefit_plan_id=benefit_plan_id, status="ACTIVE").distinct()
            beneficiary_already_active = active_beneficiaries.filter(id=id).exists() if id else False
            return active_beneficiaries.count() == max_active_beneficiaries and not beneficiary_already_active
        return False

    @register_service_signal('group_beneficiary_service.create')
    def create(self, obj_data):
        try:
            status = obj_data.get("status", None)
            benefit_plan_id = obj_data.get("benefit_plan_id", None)

            if self.would_exceed_max_active_beneficiaries(benefit_plan_id, status):
                raise ValueError(f"Error creating beneficiary with active status. Benefit plan is already at max active beneficiaries")
            return super().create(obj_data)
        except Exception as exc:
            return output_exception(model_name=self.OBJECT_TYPE.__name__, method="update", exception=exc)

    @register_service_signal('group_beneficiary_service.update')
    def update(self, obj_data):
        try:
            status = obj_data.get("status", None)
            benefit_plan_id = obj_data.get("benefit_plan_id", None)
            id = obj_data.get('id', None)

            if self.would_exceed_max_active_beneficiaries(benefit_plan_id, status, id):
                raise ValueError(f"Error changing beneficiary to active status. Benefit plan is already at max active beneficiaries")
            return super().update(obj_data)
        except Exception as exc:
            return output_exception(model_name=self.OBJECT_TYPE.__name__, method="update", exception=exc)

    @register_service_signal('group_beneficiary_service.delete')
    def delete(self, obj_data):
        return super().delete(obj_data)


class BeneficiaryImportService:
    import_loaders = {
        # .csv
        'text/csv': lambda f: pd.read_csv(f),
        # .xlsx
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': lambda f: pd.read_excel(f),
        # .xls
        'application/vnd.ms-excel': lambda f: pd.read_excel(f),
        # .ods
        'application/vnd.oasis.opendocument.spreadsheet': lambda f: pd.read_excel(f),
    }

    def __init__(self, user):
        super().__init__()
        self.user = user

    @register_service_signal('benefit_plan.import_beneficiaries')
    def import_beneficiaries(self,
                             import_file: InMemoryUploadedFile,
                             benefit_plan: BenefitPlan,
                             workflow: WorkflowHandler,
                             group_aggregation_column: str):
        upload = self._save_sources(import_file)
        self._create_benefit_plan_data_upload_records(benefit_plan, workflow, upload, group_aggregation_column)
        self._trigger_workflow(workflow, upload, benefit_plan)
        return {'success': True, 'data': {'upload_uuid': upload.uuid}}

    @transaction.atomic
    def _save_sources(self, import_file):
        # Method separated as workflow execution must be independent of the atomic transaction.
        upload = self._create_upload_entry(import_file.name)
        dataframe = self._load_import_file(import_file)
        self._validate_dataframe(dataframe)
        self._save_data_source(dataframe, upload)
        return upload

    @transaction.atomic
    def _create_benefit_plan_data_upload_records(self, benefit_plan, workflow, upload, group_aggregation_column):
        record = BenefitPlanDataUploadRecords(
            data_upload=upload,
            benefit_plan=benefit_plan,
            workflow=workflow.name,
            json_ext={"group_aggregation_column": group_aggregation_column}
        )
        record.save(user=self.user)

    def validate_import_beneficiaries(self, upload_id: uuid, individual_sources, benefit_plan: BenefitPlan):
        dataframe = self._load_dataframe(individual_sources)
        validated_dataframe, invalid_items = self._validate_possible_beneficiaries(
            dataframe,
            benefit_plan,
            upload_id
        )
        return {'success': True, 'data': validated_dataframe, 'summary_invalid_items': invalid_items}

    def create_task_with_importing_valid_items(self, upload_id: uuid, benefit_plan: BenefitPlan):
        if SocialProtectionConfig.enable_maker_checker_for_beneficiary_upload:
            BeneficiaryTaskCreatorService(self.user) \
                .create_task_with_importing_valid_items(upload_id, benefit_plan)
        else:
            record = BenefitPlanDataUploadRecords.objects.get(
                data_upload_id=upload_id,
                is_deleted=False
            )
            from social_protection.signals.on_validation_import_valid_items import IndividualItemsImportTaskCompletionEvent
            IndividualItemsImportTaskCompletionEvent(
                SocialProtectionConfig.validation_import_valid_items_workflow,
                record,
                record.data_upload.id,
                record.benefit_plan,
                self.user
            ).run_workflow()

    def create_task_with_update_valid_items(self, upload_id: uuid, benefit_plan: BenefitPlan):
        if SocialProtectionConfig.enable_maker_checker_for_beneficiary_update:
            BeneficiaryTaskCreatorService(self.user)\
                .create_task_with_update_valid_items(upload_id, benefit_plan)

        # Resolve automatically if maker-checker not enabled
        if not SocialProtectionConfig.enable_maker_checker_for_beneficiary_update:
            from social_protection.signals.on_validation_import_valid_items import IndividualItemsUploadTaskCompletionEvent
            record = BenefitPlanDataUploadRecords.objects.get(
                data_upload_id=upload_id,
                benefit_plan=benefit_plan,
                is_deleted=False
            )
            IndividualItemsUploadTaskCompletionEvent(
                SocialProtectionConfig.validation_upload_valid_items_workflow,
                record,
                record.data_upload.id,
                record.benefit_plan,
                self.user
            ).run_workflow()

    def synchronize_data_for_reporting(self, upload_id: uuid, benefit_plan: BenefitPlan):
        self._synchronize_individual(upload_id)
        self._synchronize_beneficiary(benefit_plan, upload_id)

    def _validate_possible_beneficiaries(self, dataframe: DataFrame, benefit_plan: BenefitPlan, upload_id: uuid, num_workers=4):

        if isinstance(benefit_plan.beneficiary_data_schema, str):
            schema_dict = json.loads(benefit_plan.beneficiary_data_schema)
        else:
            schema_dict = benefit_plan.beneficiary_data_schema
        properties = schema_dict.get("properties", {})

        calculation_uuid = SocialProtectionConfig.validation_calculation_uuid
        calculation = get_calculation_object(calculation_uuid)

        unique_fields = [field for field, props in properties.items() if "uniqueness" in props]
        unique_validations = {}
        if unique_fields:
            unique_validations = {
                field: dataframe[field].duplicated(keep=False)
                for field in unique_fields
            }

        chunk_size = math.ceil(len(dataframe) / num_workers)
        data_chunks = [dataframe[i:i + chunk_size] for i in range(0, dataframe.shape[0], chunk_size)]

        validated_dataframe = []
        with concurrent.futures.ProcessPoolExecutor(max_workers=num_workers) as executor:
            futures = [executor.submit(
                self.process_chunk,
                chunk,
                properties,
                unique_validations,
                calculation,
                calculation_uuid
            ) for chunk in data_chunks]

            for future in concurrent.futures.as_completed(futures):
                validated_dataframe.extend(future.result())

        self.save_validation_error_in_data_source_bulk(validated_dataframe)
        invalid_items = fetch_summary_of_broken_items(upload_id)
        return validated_dataframe, invalid_items

    @staticmethod
    def process_chunk(chunk, properties, unique_validations, calculation, calculation_uuid):
        validated_dataframe = []
        for _, row in chunk.iterrows():
            field_validation = {'row': row.to_dict(), 'validations': {}}
            for field, field_properties in properties.items():

                # Validation Calculation
                if "validationCalculation" in field_properties and field in row:
                    validation_name = field_properties["validationCalculation"]["name"]
                    field_validation['validations'][field] = calculation.calculate_if_active_for_object(
                        validation_name,
                        calculation_uuid,
                        field_name=field,
                        field_value=row[field]
                    )

                # Uniqueness Check
                if "uniqueness" in field_properties and field in row:
                    field_validation['validations'][f'{field}_uniqueness'] = {
                        'success': not unique_validations[field].loc[row.name] 
                    }
                    

            validated_dataframe.append(field_validation)

        return validated_dataframe

    def _handle_uniqueness(self, row, field, field_properties, benefit_plan, dataframe):
        unique_class_validation = SocialProtectionConfig.unique_class_validation
        calculation_uuid = SocialProtectionConfig.validation_calculation_uuid
        calculation = get_calculation_object(calculation_uuid)
        result_row = calculation.calculate_if_active_for_object(
            unique_class_validation,
            calculation_uuid,
            field_name=field,
            field_value=row[field],
            benefit_plan=benefit_plan.id,
            incoming_data=dataframe
        )
        return result_row

    def _handle_validation_calculation(self, row, field, field_properties):
        validation_calculation = field_properties.get("validationCalculation", {}).get("name")
        if not validation_calculation:
            raise ValueError("Missing validation name")
        calculation_uuid = SocialProtectionConfig.validation_calculation_uuid
        calculation = get_calculation_object(calculation_uuid)
        result_row = calculation.calculate_if_active_for_object(
            validation_calculation,
            calculation_uuid,
            field_name=field,
            field_value=row[field]
        )
        return result_row

    def _create_upload_entry(self, filename):
        upload = IndividualDataSourceUpload(source_name=filename, source_type='beneficiary import')
        upload.save(username=self.user.login_name)
        return upload

    def _validate_dataframe(self, dataframe: pd.DataFrame):
        if dataframe is None:
            raise ValueError("Unknown error while loading import file")
        if dataframe.empty:
            raise ValueError("Import file is empty")

    def _load_import_file(self, import_file) -> pd.DataFrame:
        if import_file.content_type not in self.import_loaders:
            raise ValueError("Unsupported content type: {}".format(import_file.content_type))

        return self.import_loaders[import_file.content_type](import_file)

    def _save_data_source(self, dataframe: pd.DataFrame, upload: IndividualDataSourceUpload):
        data_source_objects = []

        for _, row in dataframe.iterrows():
            ds = IndividualDataSource(
                upload=upload,
                json_ext=json.loads(row.to_json()),
                validations={},
                user_created=self.user,
                user_updated=self.user,
                uuid=uuid.uuid4()
            )
            data_source_objects.append(ds)

        IndividualDataSource.objects.bulk_create(data_source_objects)

    def _save_row(self, row, upload):
        ds = IndividualDataSource(upload=upload, json_ext=json.loads(row.to_json()), validations={})
        ds.save(username=self.user.login_name)

    def _load_dataframe(self, individual_sources) -> pd.DataFrame:
        return load_dataframe(individual_sources)

    def _trigger_workflow(self,
                          workflow: WorkflowHandler,
                          upload: IndividualDataSourceUpload,
                          benefit_plan: BenefitPlan):
        try:
            # Before the run in order to avoid racing conditions
            upload.status = IndividualDataSourceUpload.Status.TRIGGERED
            upload.save(username=self.user.login_name)

            result = workflow.run({
                # Core user UUID required
                'user_uuid': str(User.objects.get(username=self.user.login_name).id),
                'benefit_plan_uuid': str(benefit_plan.uuid),
                'upload_uuid': str(upload.uuid),
            })

            # Conditions are safety measure for workflows. Usually handles like PythonHandler or LightningHandler
            #  should follow this pattern but return type is not determined in workflow.run abstract.
            if result and isinstance(result, dict) and result.get('success') is False:
                raise ValueError(result.get('message', 'Unexpected error during the workflow execution'))
        except ValueError as e:
            upload.status = IndividualDataSourceUpload.Status.FAIL
            upload.error = {'workflow': str(e)}
            upload.save(username=self.user.login_name)
            return upload

    def save_validation_error_in_data_source_bulk(self, validated_dataframe):
        data_sources_to_update = []

        for field_validation in validated_dataframe:
            row = field_validation['row']
            error_fields = []

            for key, value in field_validation['validations'].items():
                if not value.get('success', False):
                    error_fields.append({
                        "field_name": value.get('field_name'),
                        "note": value.get('note')
                    })

            data_sources_to_update.append(
                IndividualDataSource(
                    id=row['id'],
                    validations={'validation_errors': error_fields}
                )
            )

        if data_sources_to_update:
            IndividualDataSource.objects.bulk_update(data_sources_to_update, ['validations'])

    def _synchronize_individual(self, upload_id):
        individuals_to_update = Individual.objects.filter(
            individualdatasource__upload=upload_id
        )
        for individual in individuals_to_update:
            synch_status = {
                'report_synch': 'true',
                'version': individual.version + 1,
            }
            if individual.json_ext:
                individual.json_ext.update(synch_status)
            else:
                individual.json_ext = synch_status
            individual.save(user=self.user)

    def _synchronize_beneficiary(self, benefit_plan, upload_id):
        unique_uuids = list((
            Beneficiary.objects
                .filter(benefit_plan=benefit_plan, individual__individualdatasource__upload_id=upload_id)
                .values_list('id', flat=True)
                .distinct()
        ))
        beneficiaries = Beneficiary.objects.filter(
            id__in=unique_uuids
        )
        for beneficiary in beneficiaries:
            synch_status = {
                'report_synch': 'true',
                'version': beneficiary.version + 1,
            }
            if beneficiary.json_ext:
                beneficiary.json_ext.update(synch_status)
            else:
                beneficiary.json_ext = synch_status
            beneficiary.save(user=self.user)


class BeneficiaryTaskCreatorService:

    def __init__(self, user):
        self.user = user

    def create_task_with_importing_valid_items(self, upload_id: uuid, benefit_plan: BenefitPlan):
        self._create_task(benefit_plan, upload_id, SocialProtectionConfig.validation_import_valid_items)

    def create_task_with_update_valid_items(self, upload_id: uuid, benefit_plan: BenefitPlan):
        self._create_task(benefit_plan, upload_id, SocialProtectionConfig.validation_upload_valid_items)

    @register_service_signal('socialProtection.update_task')
    @transaction.atomic()
    def _create_task(self, benefit_plan, upload_id, business_event):
        from social_protection.apps import SocialProtectionConfig
        from tasks_management.services import TaskService
        from tasks_management.apps import TasksManagementConfig
        from tasks_management.models import Task
        upload_record = BenefitPlanDataUploadRecords.objects.get(
            data_upload_id=upload_id,
            benefit_plan=benefit_plan,
            is_deleted=False
        )
        json_ext = {
            'benefit_plan_code': benefit_plan.code,
            'source_name': upload_record.data_upload.source_name,
            'workflow': upload_record.workflow,
            'percentage_of_invalid_items': self.__calculate_percentage_of_invalid_items(upload_id),
            'data_upload_id': str(upload_id)
        }
        TaskService(self.user).create({
            'source': 'import_valid_items',
            'entity': upload_record,
            'status': Task.Status.RECEIVED,
            'executor_action_event': TasksManagementConfig.default_executor_event,
            'business_event': business_event,
            'json_ext': json_ext
        })

        data_upload = upload_record.data_upload
        data_upload.status = IndividualDataSourceUpload.Status.WAITING_FOR_VERIFICATION
        data_upload.save(user=self.user)

    def __calculate_percentage_of_invalid_items(self, upload_id):
        number_of_valid_items = len(fetch_summary_of_valid_items(upload_id))
        number_of_invalid_items = len(fetch_summary_of_broken_items(upload_id))
        total_items = number_of_invalid_items + number_of_valid_items

        if total_items == 0:
            percentage_of_invalid_items = 0
        else:
            percentage_of_invalid_items = (number_of_invalid_items / total_items) * 100

        percentage_of_invalid_items = round(percentage_of_invalid_items, 2)
        return percentage_of_invalid_items


class GroupBeneficiaryImportService(BeneficiaryImportService):
    pass
    # TODO: create workflow upload/update groups and use it here