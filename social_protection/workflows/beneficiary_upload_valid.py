import logging

from core.models import User
from social_protection.workflows.utils import SqlProcedurePythonWorkflow
from social_protection.services import BeneficiaryImportService
from social_protection.models import BenefitPlan

logger = logging.getLogger(__name__)


def process_import_valid_beneficiaries_workflow(user_uuid, benefit_plan_uuid, upload_uuid, accepted=None):
    user = User.objects.get(id=user_uuid)
    benefit_plan = BenefitPlan.objects.get(id=benefit_plan_uuid)
    service = SqlProcedurePythonWorkflow(benefit_plan_uuid, upload_uuid, user_uuid, accepted)
    service.validate_dataframe_headers()
    if isinstance(accepted, list):
        service.execute(upload_sql_partial, [upload_uuid, user_uuid, benefit_plan_uuid, accepted]) \
            if benefit_plan.type == BenefitPlan.BenefitPlanType.INDIVIDUAL_TYPE \
            else service.execute(upload_sql_partial_group_type, [upload_uuid, user_uuid, benefit_plan_uuid, accepted])
    else:
        service.execute(upload_sql, [upload_uuid, user_uuid, benefit_plan_uuid]) \
            if benefit_plan.type == BenefitPlan.BenefitPlanType.INDIVIDUAL_TYPE \
            else service.execute(upload_sql_group_type, [upload_uuid, user_uuid, benefit_plan_uuid])
    BeneficiaryImportService(user).synchronize_data_for_reporting(upload_uuid, benefit_plan)
    try:
        from social_protection.operator_sync import capture_changes_from_upload
        capture_changes_from_upload(user, benefit_plan, upload_uuid, accepted=accepted)
    except Exception:
        logger.exception(
            "Failed to capture beneficiary change logs after import upload=%s", upload_uuid
        )


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
    total_entries INT;
    total_valid_entries INT;
BEGIN
    -- Check if all required fields are present in the entries
    SELECT ARRAY_AGG("UUID") INTO failing_entries_first_name
    FROM individual_individualdatasource
    WHERE upload_id = current_upload_id AND individual_id IS NULL AND "isDeleted" = False AND NOT "Json_ext" ? 'first_name';

    SELECT ARRAY_AGG("UUID") INTO failing_entries_last_name
    FROM individual_individualdatasource
    WHERE upload_id = current_upload_id AND individual_id IS NULL AND "isDeleted" = False AND NOT "Json_ext" ? 'last_name';

    SELECT ARRAY_AGG("UUID") INTO failing_entries_dob
    FROM individual_individualdatasource
    WHERE upload_id = current_upload_id AND individual_id IS NULL AND "isDeleted" = False AND NOT "Json_ext" ? 'dob';

    -- Check if any entries have invalid Json_ext according to the schema
    SELECT beneficiary_data_schema INTO json_schema FROM social_protection_benefitplan WHERE "UUID" = benefitPlan;
    SELECT ARRAY_AGG("UUID") INTO failing_entries_invalid_json
    FROM individual_individualdatasource
    WHERE upload_id = current_upload_id AND individual_id IS NULL AND "isDeleted" = False AND NOT validate_json_schema(json_schema, "Json_ext");

    -- If any entries do not meet the criteria or missing required fields, set the error message in the upload table and do not proceed further
    IF failing_entries_invalid_json IS NOT NULL OR failing_entries_first_name IS NOT NULL OR failing_entries_last_name IS NOT NULL OR failing_entries_dob IS NOT NULL THEN
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
        
        UPDATE individual_individualdatasourceupload SET status = 'FAIL' WHERE "UUID" = current_upload_id;
    ELSE
        -- If no invalid entries, then proceed with the data manipulation
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
            WHERE ds.upload_id = current_upload_id
                AND ds.individual_id IS NULL 
                AND ds."isDeleted" = False 
                AND ds.validations ->> 'validation_errors' = '[]'
            RETURNING "UUID", "Json_ext"
        )
        UPDATE individual_individualdatasource
        SET individual_id = ne."UUID"
        FROM new_entry ne
        WHERE individual_individualdatasource.upload_id = current_upload_id
          AND individual_individualdatasource.individual_id IS NULL
          AND individual_individualdatasource."isDeleted" = False
          AND individual_individualdatasource."Json_ext" = ne."Json_ext"
          AND validations ->> 'validation_errors' = '[]';
        
        with new_entry_2 as (INSERT INTO social_protection_beneficiary(
        "UUID", "isDeleted", "Json_ext", "DateCreated", "DateUpdated", version, "DateValidFrom", "DateValidTo", status, "benefit_plan_id", "individual_id", "UserCreatedUUID", "UserUpdatedUUID"
        )
        SELECT gen_random_uuid(), false, iids."Json_ext" - 'first_name' - 'last_name' - 'dob', NOW(), NOW(), 1, NOW(), NULL, 'POTENTIAL', benefitPlan, new_entry."UUID", userUUID, userUUID
        FROM individual_individualdatasource iids right join individual_individual new_entry on new_entry."UUID" = iids.individual_id
        WHERE iids.upload_id=current_upload_id and iids."isDeleted"=false
        returning "UUID")
        
        -- Calculate counts of valid and total entries
        SELECT count(*) INTO total_valid_entries
        FROM individual_individualdatasource
        WHERE upload_id = current_upload_id
          AND "isDeleted" = FALSE
          AND COALESCE(validations ->> 'validation_errors', '[]') = '[]';
        SELECT count(*) INTO total_entries
        FROM individual_individualdatasource
        WHERE upload_id = current_upload_id
          AND "isDeleted" = FALSE;
        
        -- Change status to SUCCESS if no invalid items, change to PARTIAL_SUCCESS otherwise 
            UPDATE individual_individualdatasourceupload
            SET 
                status = CASE
                    WHEN total_valid_entries = total_entries THEN 'SUCCESS'
                    ELSE 'PARTIAL_SUCCESS'
                END,
                error = CASE
                    WHEN total_valid_entries < total_entries THEN jsonb_build_object(
                        'error', 'Partial success due to some invalid entries',
                        'timestamp', NOW()::text,
                        'upload_id', current_upload_id::text,
                        'total_valid_entries', total_valid_entries,
                        'total_entries', total_entries
                    )
                    ELSE '{}'
                END
            WHERE "UUID" = current_upload_id;
    END IF;
EXCEPTION WHEN OTHERS THEN
    UPDATE individual_individualdatasourceupload SET status = 'FAIL', error = jsonb_build_object(
        'error', SQLERRM,
        'timestamp', NOW()::text,
        'upload_id', current_upload_id::text
    )
    WHERE "UUID" = current_upload_id;
END $$;

"""

upload_sql_partial = """
DO $$
DECLARE
    current_upload_id UUID := %s::UUID;
    userUUID UUID := %s::UUID;
    benefitPlan UUID := %s::UUID;
    accepted UUID[] := %s::UUID[]; -- Placeholder for the accepted UUIDs array, can be NULL
    failing_entries UUID[];
    json_schema jsonb;
    failing_entries_invalid_json UUID[];
    failing_entries_first_name UUID[];
    failing_entries_last_name UUID[];
    failing_entries_dob UUID[];
BEGIN
    -- Check if all required fields are present in the entries, with accepted filter applied if not NULL
    SELECT ARRAY_AGG("UUID") INTO failing_entries_first_name
    FROM individual_individualdatasource
    WHERE upload_id = current_upload_id AND individual_id IS NULL AND "isDeleted" = False AND NOT "Json_ext" ? 'first_name'
    AND (accepted IS NULL OR "UUID" = ANY(accepted));

    SELECT ARRAY_AGG("UUID") INTO failing_entries_last_name
    FROM individual_individualdatasource
    WHERE upload_id = current_upload_id AND individual_id IS NULL AND "isDeleted" = False AND NOT "Json_ext" ? 'last_name'
    AND (accepted IS NULL OR "UUID" = ANY(accepted));

    SELECT ARRAY_AGG("UUID") INTO failing_entries_dob
    FROM individual_individualdatasource
    WHERE upload_id = current_upload_id AND individual_id IS NULL AND "isDeleted" = False AND NOT "Json_ext" ? 'dob'
    AND (accepted IS NULL OR "UUID" = ANY(accepted));

    -- Check if any entries have invalid Json_ext according to the schema, with accepted filter applied if not NULL
    SELECT ARRAY_AGG("UUID") INTO failing_entries_invalid_json
    FROM individual_individualdatasource
    WHERE upload_id = current_upload_id AND individual_id IS NULL AND "isDeleted" = False AND NOT validate_json_schema(json_schema, "Json_ext")
    AND (accepted IS NULL OR "UUID" = ANY(accepted));

    -- If any entries do not meet the criteria or missing required fields, set the error message in the upload table and do not proceed further
    IF failing_entries_invalid_json IS NOT NULL OR failing_entries_first_name IS NOT NULL OR failing_entries_last_name IS NOT NULL OR failing_entries_dob IS NOT NULL THEN
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

        UPDATE individual_individualdatasourceupload SET status = 'FAIL' WHERE "UUID" = current_upload_id;
    ELSE
        -- If no invalid entries, then proceed with the data manipulation, considering the accepted filter
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
            WHERE ds.upload_id = current_upload_id 
                AND ds.individual_id IS NULL
                AND ds."isDeleted" = False
                AND ds.validations ->> 'validation_errors' = '[]'
            AND (accepted IS NULL OR "UUID" = ANY(accepted))
            RETURNING "UUID", "Json_ext"
        )
        UPDATE individual_individualdatasource
        SET individual_id = ne."UUID"
        FROM new_entry ne
        WHERE individual_individualdatasource.upload_id = current_upload_id
          AND individual_individualdatasource.individual_id IS NULL
          AND individual_individualdatasource."isDeleted" = False
          AND individual_individualdatasource."Json_ext" = ne."Json_ext"
          AND validations ->> 'validation_errors' = '[]'
          AND (accepted IS NULL OR individual_individualdatasource."UUID" = ANY(accepted));

        INSERT INTO social_protection_beneficiary(
        "UUID", "isDeleted", "Json_ext", "DateCreated", "DateUpdated", version, "DateValidFrom", "DateValidTo", status, "benefit_plan_id", "individual_id", "UserCreatedUUID", "UserUpdatedUUID"
        )
        SELECT gen_random_uuid(), false, iids."Json_ext" - 'first_name' - 'last_name' - 'dob', NOW(), NOW(), 1, NOW(), NULL, 'POTENTIAL', benefitPlan, new_entry."UUID", userUUID, userUUID
        FROM individual_individualdatasource iids right join individual_individual new_entry on new_entry."UUID" = iids.individual_id
        WHERE iids.upload_id=current_upload_id and iids."isDeleted"=false
        AND (accepted IS NULL OR iids."UUID" = ANY(accepted));
    END IF;
EXCEPTION WHEN OTHERS THEN
    UPDATE individual_individualdatasourceupload SET status = 'FAIL', error = jsonb_build_object(
        'error', SQLERRM,
        'timestamp', NOW()::text,
        'upload_id', current_upload_id::text
    )
    WHERE "UUID" = current_upload_id;
END $$;

"""


upload_sql_group_type = """
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
    total_entries INT;
    total_valid_entries INT;
BEGIN
    -- Check if all required fields are present in the entries
    SELECT ARRAY_AGG("UUID") INTO failing_entries_first_name
    FROM individual_individualdatasource
    WHERE upload_id = current_upload_id AND individual_id IS NULL AND "isDeleted" = False AND NOT "Json_ext" ? 'first_name';

    SELECT ARRAY_AGG("UUID") INTO failing_entries_last_name
    FROM individual_individualdatasource
    WHERE upload_id = current_upload_id AND individual_id IS NULL AND "isDeleted" = False AND NOT "Json_ext" ? 'last_name';

    SELECT ARRAY_AGG("UUID") INTO failing_entries_dob
    FROM individual_individualdatasource
    WHERE upload_id = current_upload_id AND individual_id IS NULL AND "isDeleted" = False AND NOT "Json_ext" ? 'dob';

    -- Check if any entries have invalid Json_ext according to the schema
    SELECT beneficiary_data_schema INTO json_schema FROM social_protection_benefitplan WHERE "UUID" = benefitPlan;
    SELECT ARRAY_AGG("UUID") INTO failing_entries_invalid_json
    FROM individual_individualdatasource
    WHERE upload_id = current_upload_id AND individual_id IS NULL AND "isDeleted" = False AND NOT validate_json_schema(json_schema, "Json_ext");

    -- If any entries do not meet the criteria or missing required fields, set the error message in the upload table and do not proceed further
    IF failing_entries_invalid_json IS NOT NULL OR failing_entries_first_name IS NOT NULL OR failing_entries_last_name IS NOT NULL OR failing_entries_dob IS NOT NULL THEN
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

        UPDATE individual_individualdatasourceupload SET status = 'FAIL' WHERE "UUID" = current_upload_id;
    ELSE
        -- If no invalid entries, then proceed with the data manipulation
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
            WHERE ds.upload_id = current_upload_id
                AND ds.individual_id IS NULL 
                AND ds."isDeleted" = False 
                AND ds.validations ->> 'validation_errors' = '[]'
            RETURNING "UUID", "Json_ext"
        )
        UPDATE individual_individualdatasource
        SET individual_id = ne."UUID"
        FROM new_entry ne
        WHERE individual_individualdatasource.upload_id = current_upload_id
          AND individual_individualdatasource.individual_id IS NULL
          AND individual_individualdatasource."isDeleted" = False
          AND individual_individualdatasource."Json_ext" = ne."Json_ext"
          AND validations ->> 'validation_errors' = '[]';

        -- Calculate counts of valid and total entries
        SELECT count(*) INTO total_valid_entries
        FROM individual_individualdatasource
        WHERE upload_id = current_upload_id
          AND "isDeleted" = FALSE
          AND COALESCE(validations ->> 'validation_errors', '[]') = '[]';
        SELECT count(*) INTO total_entries
        FROM individual_individualdatasource
        WHERE upload_id = current_upload_id
          AND "isDeleted" = FALSE;

        -- Change status to SUCCESS if no invalid items, change to PARTIAL_SUCCESS otherwise 
            UPDATE individual_individualdatasourceupload
            SET 
                status = CASE
                    WHEN total_valid_entries = total_entries THEN 'SUCCESS'
                    ELSE 'PARTIAL_SUCCESS'
                END,
                error = CASE
                    WHEN total_valid_entries < total_entries THEN jsonb_build_object(
                        'error', 'Partial success due to some invalid entries',
                        'timestamp', NOW()::text,
                        'upload_id', current_upload_id::text,
                        'total_valid_entries', total_valid_entries,
                        'total_entries', total_entries
                    )
                    ELSE '{}'
                END
            WHERE "UUID" = current_upload_id;
    END IF;
EXCEPTION WHEN OTHERS THEN
    UPDATE individual_individualdatasourceupload SET status = 'FAIL', error = jsonb_build_object(
        'error', SQLERRM,
        'timestamp', NOW()::text,
        'upload_id', current_upload_id::text
    )
    WHERE "UUID" = current_upload_id;
END $$;

"""


upload_sql_partial_group_type = """
DO $$
DECLARE
    current_upload_id UUID := %s::UUID;
    userUUID UUID := %s::UUID;
    benefitPlan UUID := %s::UUID;
    accepted UUID[] := %s::UUID[]; -- Placeholder for the accepted UUIDs array, can be NULL
    failing_entries UUID[];
    json_schema jsonb;
    failing_entries_invalid_json UUID[];
    failing_entries_first_name UUID[];
    failing_entries_last_name UUID[];
    failing_entries_dob UUID[];
BEGIN
    -- Check if all required fields are present in the entries, with accepted filter applied if not NULL
    SELECT ARRAY_AGG("UUID") INTO failing_entries_first_name
    FROM individual_individualdatasource
    WHERE upload_id = current_upload_id AND individual_id IS NULL AND "isDeleted" = False AND NOT "Json_ext" ? 'first_name'
    AND (accepted IS NULL OR "UUID" = ANY(accepted));

    SELECT ARRAY_AGG("UUID") INTO failing_entries_last_name
    FROM individual_individualdatasource
    WHERE upload_id = current_upload_id AND individual_id IS NULL AND "isDeleted" = False AND NOT "Json_ext" ? 'last_name'
    AND (accepted IS NULL OR "UUID" = ANY(accepted));

    SELECT ARRAY_AGG("UUID") INTO failing_entries_dob
    FROM individual_individualdatasource
    WHERE upload_id = current_upload_id AND individual_id IS NULL AND "isDeleted" = False AND NOT "Json_ext" ? 'dob'
    AND (accepted IS NULL OR "UUID" = ANY(accepted));

    -- Check if any entries have invalid Json_ext according to the schema, with accepted filter applied if not NULL
    SELECT ARRAY_AGG("UUID") INTO failing_entries_invalid_json
    FROM individual_individualdatasource
    WHERE upload_id = current_upload_id AND individual_id IS NULL AND "isDeleted" = False AND NOT validate_json_schema(json_schema, "Json_ext")
    AND (accepted IS NULL OR "UUID" = ANY(accepted));

    -- If any entries do not meet the criteria or missing required fields, set the error message in the upload table and do not proceed further
    IF failing_entries_invalid_json IS NOT NULL OR failing_entries_first_name IS NOT NULL OR failing_entries_last_name IS NOT NULL OR failing_entries_dob IS NOT NULL THEN
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

        UPDATE individual_individualdatasourceupload SET status = 'FAIL' WHERE "UUID" = current_upload_id;
    ELSE
        -- If no invalid entries, then proceed with the data manipulation, considering the accepted filter
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
            WHERE ds.upload_id = current_upload_id 
                AND ds.individual_id IS NULL
                AND ds."isDeleted" = False
                AND ds.validations ->> 'validation_errors' = '[]'
            AND (accepted IS NULL OR "UUID" = ANY(accepted))
            RETURNING "UUID", "Json_ext"
        )
        UPDATE individual_individualdatasource
        SET individual_id = ne."UUID"
        FROM new_entry ne
        WHERE individual_individualdatasource.upload_id = current_upload_id
          AND individual_individualdatasource.individual_id IS NULL
          AND individual_individualdatasource."isDeleted" = False
          AND individual_individualdatasource."Json_ext" = ne."Json_ext"
          AND validations ->> 'validation_errors' = '[]'
          AND (accepted IS NULL OR individual_individualdatasource."UUID" = ANY(accepted));

    END IF;
EXCEPTION WHEN OTHERS THEN
    UPDATE individual_individualdatasourceupload SET status = 'FAIL', error = jsonb_build_object(
        'error', SQLERRM,
        'timestamp', NOW()::text,
        'upload_id', current_upload_id::text
    )
    WHERE "UUID" = current_upload_id;
END $$;

"""