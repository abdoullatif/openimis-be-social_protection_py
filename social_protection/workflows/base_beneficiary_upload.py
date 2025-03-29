import logging

from core.models import User
from social_protection.workflows.utils import DataUploadWorkflow
from social_protection.services import BeneficiaryImportService
from social_protection.models import BenefitPlan
from django.db import connection

logger = logging.getLogger(__name__)


def process_import_beneficiaries_workflow(user_uuid, benefit_plan_uuid, upload_uuid):
    # Call the records' validation service directly with the provided arguments
    user = User.objects.get(id=user_uuid)
    benefit_plan = BenefitPlan.objects.get(id=benefit_plan_uuid)
    service = DataUploadWorkflow(benefit_plan_uuid, upload_uuid, user_uuid)
    service.validate_dataframe_headers()
    if benefit_plan.type == BenefitPlan.BenefitPlanType.INDIVIDUAL_TYPE:
        with connection.cursor() as cursor:
            cursor.execute("SELECT process_import_beneficiaries(%s, %s, %s)",
                           [upload_uuid, user_uuid, benefit_plan_uuid])
        # service.execute(upload_sql)
    else:
        # service.execute(upload_sql_group_version)
        with connection.cursor() as cursor:
            cursor.execute("SELECT process_import_beneficiaries_group(%s, %s, %s)",
                           [upload_uuid, user_uuid, benefit_plan_uuid])
    BeneficiaryImportService(user).synchronize_data_for_reporting(upload_uuid, benefit_plan)


upload_sql = """
DO $$
 DECLARE
            current_upload_id UUID := %s::UUID;
            userUUID UUID := %s::UUID;
            benefitPlan UUID := %s::UUID;
            failing_entries UUID[];
            json_schema jsonb;
            failing_entries_invalid_json UUID[];
            failing_entries_first_name UUID[];
            failing_entries_last_name UUID[];
            failing_entries_dob UUID[];
            BEGIN
    -- Check if all required fields are present in the entries
    SELECT ARRAY_AGG("UUID") INTO failing_entries_first_name
    FROM individual_individualdatasource
    WHERE upload_id=current_upload_id and individual_id is null and "isDeleted"=False AND NOT "Json_ext" ? 'first_name';

    SELECT ARRAY_AGG("UUID") INTO failing_entries_last_name
    FROM individual_individualdatasource
    WHERE upload_id=current_upload_id and individual_id is null and "isDeleted"=False AND NOT "Json_ext" ? 'last_name';

    SELECT ARRAY_AGG("UUID") INTO failing_entries_dob
    FROM individual_individualdatasource
    WHERE upload_id=current_upload_id and individual_id is null and "isDeleted"=False AND NOT "Json_ext" ? 'dob';


    -- Check if any entries have invalid Json_ext according to the schema
    SELECT beneficiary_data_schema INTO json_schema FROM social_protection_benefitplan WHERE "UUID" = benefitPlan;
    SELECT ARRAY_AGG("UUID") INTO failing_entries_invalid_json
    FROM individual_individualdatasource
    WHERE upload_id=current_upload_id and individual_id is null and "isDeleted"=False AND NOT validate_json_schema(json_schema, "Json_ext");

    -- If any entries do not meet the criteria or missing required fields, set the error message in the upload table and do not proceed further
    IF failing_entries_invalid_json IS NOT NULL or failing_entries_first_name IS NOT NULL OR failing_entries_last_name IS NOT NULL OR failing_entries_dob IS NOT NULL THEN
        UPDATE individual_individualdatasourceupload
        SET error = coalesce(error, '{}'::jsonb) || jsonb_build_object('errors', jsonb_build_object(
                            'error', 'Invalid entries',
                            'timestamp', NOW()::text,
                            'upload_id', current_upload_id::text,
                            'failing_entries_first_name', failing_entries_first_name,
                            'failing_entries_last_name', failing_entries_last_name,
                            'failing_entries_dob', failing_entries_dob,
                            'failing_entries_invalid_json', failing_entries_invalid_json
                        ))
        WHERE "UUID" = current_upload_id;

       update individual_individualdatasourceupload set status='FAIL' where "UUID" = current_upload_id;
    -- If no invalid entries, then proceed with the data manipulation
    ELSE
        BEGIN
          WITH new_entry AS (
            INSERT INTO individual_individual(
            "UUID", "isDeleted", version, "UserCreatedUUID", "UserUpdatedUUID",
            "Json_ext", first_name, last_name, dob, location_id
            )
            SELECT gen_random_uuid(), false, 1, userUUID, userUUID,
                "Json_ext",
                "Json_ext"->>'first_name',
                "Json_ext" ->> 'last_name',
                to_date("Json_ext" ->> 'dob', 'YYYY-MM-DD'),
                loc."LocationId"
            FROM individual_individualdatasource AS ds
            LEFT JOIN "tblLocations" AS loc
                    ON loc."LocationName" = ds."Json_ext"->>'location_name'
                    AND loc."LocationCode" = ds."Json_ext"->>'location_code'
                    AND loc."LocationType"='V'
                    AND loc."ValidityTo" IS NULL
            WHERE ds.upload_id=current_upload_id 
                AND ds.individual_id is null
                AND ds."isDeleted"=False
            RETURNING "UUID", "Json_ext"  -- also return the Json_ext
          )
          UPDATE individual_individualdatasource
          SET individual_id = new_entry."UUID"
          FROM new_entry
          WHERE upload_id=current_upload_id
            and individual_id is null
            and "isDeleted"=False
            and individual_individualdatasource."Json_ext" = new_entry."Json_ext";  -- match on Json_ext


            with new_entry_2 as (INSERT INTO social_protection_beneficiary(
            "UUID", "isDeleted", "Json_ext", "DateCreated", "DateUpdated", version, "DateValidFrom", "DateValidTo", status, "benefit_plan_id", "individual_id", "UserCreatedUUID", "UserUpdatedUUID"
            )
            SELECT gen_random_uuid(), false, iids."Json_ext" - 'first_name' - 'last_name' - 'dob', NOW(), NOW(), 1, NOW(), NULL, 'POTENTIAL', benefitPlan, new_entry."UUID", userUUID, userUUID
            FROM individual_individualdatasource iids right join individual_individual new_entry on new_entry."UUID" = iids.individual_id
            WHERE iids.upload_id=current_upload_id and iids."isDeleted"=false
            returning "UUID")


            update individual_individualdatasourceupload set status='SUCCESS', error='{}' where "UUID" = current_upload_id;
            EXCEPTION
            WHEN OTHERS then

            update individual_individualdatasourceupload set status='FAIL' where "UUID" = current_upload_id;
                UPDATE individual_individualdatasourceupload
                SET error = coalesce(error, '{}'::jsonb) || jsonb_build_object('errors', jsonb_build_object(
                                    'error', SQLERRM,
                                    'timestamp', NOW()::text,
                                    'upload_id', current_upload_id::text
                                ))
                WHERE "UUID" = current_upload_id;
        END;
    END IF;
END $$;
        """


upload_sql_group_version = """
DO $$
 DECLARE
            current_upload_id UUID := %s::UUID;
            userUUID UUID := %s::UUID;
            benefitPlan UUID := %s::UUID;
            failing_entries UUID[];
            json_schema jsonb;
            failing_entries_invalid_json UUID[];
            failing_entries_first_name UUID[];
            failing_entries_last_name UUID[];
            failing_entries_dob UUID[];
            BEGIN
    -- Check if all required fields are present in the entries
    SELECT ARRAY_AGG("UUID") INTO failing_entries_first_name
    FROM individual_individualdatasource
    WHERE upload_id=current_upload_id and individual_id is null and "isDeleted"=False AND NOT "Json_ext" ? 'first_name';

    SELECT ARRAY_AGG("UUID") INTO failing_entries_last_name
    FROM individual_individualdatasource
    WHERE upload_id=current_upload_id and individual_id is null and "isDeleted"=False AND NOT "Json_ext" ? 'last_name';

    SELECT ARRAY_AGG("UUID") INTO failing_entries_dob
    FROM individual_individualdatasource
    WHERE upload_id=current_upload_id and individual_id is null and "isDeleted"=False AND NOT "Json_ext" ? 'dob';


    -- Check if any entries have invalid Json_ext according to the schema
    SELECT beneficiary_data_schema INTO json_schema FROM social_protection_benefitplan WHERE "UUID" = benefitPlan;
    SELECT ARRAY_AGG("UUID") INTO failing_entries_invalid_json
    FROM individual_individualdatasource
    WHERE upload_id=current_upload_id and individual_id is null and "isDeleted"=False AND NOT validate_json_schema(json_schema, "Json_ext");

    -- If any entries do not meet the criteria or missing required fields, set the error message in the upload table and do not proceed further
    IF failing_entries_invalid_json IS NOT NULL or failing_entries_first_name IS NOT NULL OR failing_entries_last_name IS NOT NULL OR failing_entries_dob IS NOT NULL THEN
        UPDATE individual_individualdatasourceupload
        SET error = coalesce(error, '{}'::jsonb) || jsonb_build_object('errors', jsonb_build_object(
                            'error', 'Invalid entries',
                            'timestamp', NOW()::text,
                            'upload_id', current_upload_id::text,
                            'failing_entries_first_name', failing_entries_first_name,
                            'failing_entries_last_name', failing_entries_last_name,
                            'failing_entries_dob', failing_entries_dob,
                            'failing_entries_invalid_json', failing_entries_invalid_json
                        ))
        WHERE "UUID" = current_upload_id;

       update individual_individualdatasourceupload set status='FAIL' where "UUID" = current_upload_id;
    -- If no invalid entries, then proceed with the data manipulation
    ELSE
        BEGIN
          WITH new_entry AS (
            INSERT INTO individual_individual(
            "UUID", "isDeleted", version, "UserCreatedUUID", "UserUpdatedUUID",
            "Json_ext", first_name, last_name, dob, location_id
            )
            SELECT gen_random_uuid(), false, 1, userUUID, userUUID,
                "Json_ext",
                "Json_ext"->>'first_name',
                "Json_ext" ->> 'last_name',
                to_date("Json_ext" ->> 'dob', 'YYYY-MM-DD'),
                loc."LocationId"
            FROM individual_individualdatasource AS ds
            LEFT JOIN "tblLocations" AS loc
                    ON loc."LocationName" = ds."Json_ext"->>'location_name'
                    AND loc."LocationCode" = ds."Json_ext"->>'location_code'
                    AND loc."LocationType"='V'
                    AND loc."ValidityTo" IS NULL
            WHERE ds.upload_id=current_upload_id 
                AND ds.individual_id is null
                AND ds."isDeleted"=False
            RETURNING "UUID", "Json_ext"  -- also return the Json_ext
          )
          UPDATE individual_individualdatasource
          SET individual_id = new_entry."UUID"
          FROM new_entry
          WHERE upload_id=current_upload_id
            and individual_id is null
            and "isDeleted"=False
            and individual_individualdatasource."Json_ext" = new_entry."Json_ext";  -- match on Json_ext

            update individual_individualdatasourceupload set status='SUCCESS', error='{}' where "UUID" = current_upload_id;
            EXCEPTION
            WHEN OTHERS then

            update individual_individualdatasourceupload set status='FAIL' where "UUID" = current_upload_id;
                UPDATE individual_individualdatasourceupload
                SET error = coalesce(error, '{}'::jsonb) || jsonb_build_object('errors', jsonb_build_object(
                                    'error', SQLERRM,
                                    'timestamp', NOW()::text,
                                    'upload_id', current_upload_id::text
                                ))
                WHERE "UUID" = current_upload_id;
        END;
    END IF;
END $$;
        """