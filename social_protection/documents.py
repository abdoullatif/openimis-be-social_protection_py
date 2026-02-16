from django.apps import apps
from django.conf import settings

is_unit_test_env = getattr(settings, 'IS_UNIT_TEST_ENV', False)

# Check if the 'opensearch_reports' app is in INSTALLED_APPS
if 'opensearch_reports' in apps.app_configs and not is_unit_test_env:
    from opensearch_reports.service import BaseSyncDocument
    from django_opensearch_dsl import fields as opensearch_fields
    from django_opensearch_dsl.registries import registry
    from social_protection.models import Beneficiary, BenefitPlan
    from individual.models import Individual

    @registry.register_document
    class BeneficiaryDocument(BaseSyncDocument):
        DASHBOARD_NAME = 'Beneficiary'

        benefit_plan = opensearch_fields.ObjectField(properties={
            'code': opensearch_fields.KeywordField(),
            'name': opensearch_fields.KeywordField(),
        })
        individual = opensearch_fields.ObjectField(properties={
            'first_name': opensearch_fields.KeywordField(),
            'last_name': opensearch_fields.KeywordField(),
            'dob': opensearch_fields.DateField(),
        })
        status = opensearch_fields.KeywordField(fields={
            'status_key': opensearch_fields.KeywordField()}
        )
        date_created = opensearch_fields.DateField()
        json_ext = opensearch_fields.ObjectField()
        location = opensearch_fields.ObjectField(properties={
            'code': opensearch_fields.KeywordField(),
            'name': opensearch_fields.KeywordField(),
            'type': opensearch_fields.KeywordField(),
            'region': opensearch_fields.KeywordField(),
            'prefecture': opensearch_fields.KeywordField(),
            'sous_prefecture': opensearch_fields.KeywordField(),
            'district': opensearch_fields.KeywordField(),
        })

        class Index:
            name = 'beneficiary'
            settings = {
                'number_of_shards': 1,
                'number_of_replicas': 0
            }
            auto_refresh = True

        class Django:
            model = Beneficiary
            related_models = [BenefitPlan, Individual]
            fields = [
                'id'
            ]
            queryset_pagination = 5000

        def get_instances_from_related(self, related_instance):
            if isinstance(related_instance, BenefitPlan):
                return Beneficiary.objects.filter(benefit_plan=related_instance)
            elif isinstance(related_instance, Individual):
                return Beneficiary.objects.filter(individual=related_instance)

        def prepare_json_ext(self, instance):
            json_ext_data = instance.json_ext
            json_data = self.__flatten_dict(json_ext_data)
            return json_data

        def prepare_location(self, instance):
            individual = getattr(instance, 'individual', None)
            location = getattr(individual, 'location', None)
            if not location and individual:
                group_rel = individual.groupindividuals.select_related('group__location').first()
                if group_rel and group_rel.group and group_rel.group.location:
                    location = group_rel.group.location

            if not location and individual and getattr(individual, 'json_ext', None):
                json_ext = individual.json_ext or {}
                return {
                    'region': json_ext.get('region'),
                    'prefecture': json_ext.get('prefecture'),
                    'sous_prefecture': json_ext.get('sous_prefecture'),
                    'district': json_ext.get('district'),
                }

            if not location:
                return None

            data = {
                'code': location.code,
                'name': location.name,
                'type': location.type,
            }

            current = location
            while current:
                loc_type = getattr(current, 'type', None)
                if loc_type == 'R':
                    data.setdefault('region', current.name)
                    data.setdefault('prefecture', current.name)
                elif loc_type == 'P':
                    data.setdefault('prefecture', current.name)
                elif loc_type == 'S':
                    data.setdefault('sous_prefecture', current.name)
                elif loc_type == 'D':
                    data.setdefault('district', current.name)
                elif loc_type == 'W':
                    data.setdefault('sous_prefecture', current.name)
                current = current.parent

            return data

        def __flatten_dict(self, d, parent_key='', sep='__'):
            items = {}
            for k, v in d.items():
                new_key = f"{parent_key}{sep}{k}" if parent_key else k
                if isinstance(v, dict):
                    items.update(self.__flatten_dict(v, new_key, sep=sep))
                else:
                    items[new_key] = v
            return items

