from django.core.management.base import BaseCommand
from social_protection.models import Beneficiary
from social_protection.documents import BeneficiaryDocument


class Command(BaseCommand):
    help = 'Imports beneficiary data from openIMIS into OpenSearch. ' \
           'This command should be executed within the openimis-be_py module, which ' \
           'is part of the openIMIS module, using the manage.py command. For example, you can run: ' \
           'python manage.py add_beneficiary_data_to_opensearch. ' \
           'This command creates or updates data documents at the OpenSearch level.'

    def handle(self, *args, **options):
        # Initialize the index
        BeneficiaryDocument.init(index='beneficiary')
        # Loop through Beneficiary objects and index them
        for beneficiary in Beneficiary.objects.all():
            location_data = BeneficiaryDocument().prepare_location(beneficiary)
            beneficiary_document = BeneficiaryDocument(
                meta={'id': beneficiary.id},  # Set the ID
                benefit_plan={
                    'code': beneficiary.benefit_plan.code,
                    'name': beneficiary.benefit_plan.name,
                },
                individual={
                    'first_name': beneficiary.individual.first_name,
                    'last_name': beneficiary.individual.last_name,
                    'dob': beneficiary.individual.dob,
                },
                location=location_data,
                id=beneficiary.id,
                status=beneficiary.status,
                json_ext=beneficiary.json_ext,
                date_created=str(beneficiary.date_created)
            )
            # Save the BeneficiaryDocument to index it in OpenSearch
            result = beneficiary_document.save()
            self.stdout.write(self.style.SUCCESS(result))
