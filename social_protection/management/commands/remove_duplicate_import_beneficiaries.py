"""
Supprime en toute sécurité le 2e lot d'un double import CSV (bénéficiaires les plus récents par ménage).

Usage:
  python manage.py remove_duplicate_import_beneficiaries --benefit-plan-code PL004
  python manage.py remove_duplicate_import_beneficiaries --benefit-plan-code PL004 --execute
  python manage.py remove_duplicate_import_beneficiaries --benefit-plan-code PL004 --execute --purge-individuals
"""

from collections import Counter, defaultdict
from datetime import datetime

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from contribution_plan.models import PaymentPlan
from core.models import User
from payroll.models import BenefitConsumption, PayrollBenefitConsumption
from social_protection.models import Beneficiary, BeneficiaryStatus, BenefitPlan


class Command(BaseCommand):
    help = (
        "Retire le 2e lot d'un double import (1 bénéficiaire / ménage conservé). "
        "Dry-run par défaut ; ajouter --execute pour appliquer."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--benefit-plan-code",
            default="PL004",
            help="Code du régime (BenefitPlan) lié au plan de paiement (défaut: PL004).",
        )
        parser.add_argument(
            "--second-lot-after",
            default="2026-05-20 10:50:00",
            help="Date/heure min pour identifier le 2e lot (doit correspondre au 2e import).",
        )
        parser.add_argument(
            "--username",
            default="DevOps",
            help="Utilisateur openIMIS pour l'audit de suppression (HistoryModel).",
        )
        parser.add_argument(
            "--execute",
            action="store_true",
            help="Appliquer les suppressions (sinon simulation uniquement).",
        )
        parser.add_argument(
            "--purge-individuals",
            action="store_true",
            help="Après suppression bénéficiaire, supprimer l'individu orphelin (2e lot uniquement).",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=2000,
            help="Taille des lots pour les transactions.",
        )
        parser.add_argument(
            "--fast",
            action="store_true",
            help="Soft-delete en masse (plus rapide, sans historique ligne à ligne).",
        )

    def handle(self, *args, **options):
        code = options["benefit_plan_code"]
        cutoff = self._normalize_dt(self._parse_cutoff(options["second_lot_after"]))
        execute = options["execute"]
        purge_individuals = options["purge_individuals"]
        batch_size = options["batch_size"]
        fast = options["fast"]

        user = User.objects.filter(username=options["username"]).first()
        if not user and execute:
            self.stderr.write(self.style.ERROR(f"User '{options['username']}' introuvable."))
            return

        payment_plan = PaymentPlan.objects.filter(code=code, is_deleted=False).first()
        if not payment_plan:
            self.stderr.write(self.style.ERROR(f"PaymentPlan '{code}' introuvable."))
            return

        benefit_plan = BenefitPlan.objects.filter(
            id=payment_plan.benefit_plan_id, is_deleted=False
        ).first()
        if not benefit_plan:
            self.stderr.write(self.style.ERROR("BenefitPlan lié introuvable."))
            return

        self.stdout.write(
            f"Régime: {benefit_plan.code} ({benefit_plan.id}) — plan {payment_plan.code}"
        )
        self.stdout.write(f"Seuil 2e lot (date_created >=): {cutoff}")

        candidates = self._resolve_second_lot_candidates(benefit_plan.id, cutoff)
        if not candidates:
            self.stdout.write(self.style.WARNING("Aucun candidat à supprimer."))
            return

        blocked = self._find_blocked(candidates)
        if blocked:
            self.stderr.write(
                self.style.ERROR(
                    f"{len(blocked)} bénéficiaire(s) bloqué(s) (paie / autre régime). "
                    "Suppression annulée."
                )
            )
            for item in blocked[:10]:
                self.stderr.write(f"  - {item}")
            if len(blocked) > 10:
                self.stderr.write(f"  … et {len(blocked) - 10} autres")
            return

        self.stdout.write(self.style.SUCCESS(f"Candidats 2e lot: {len(candidates)}"))
        active_after = (
            Beneficiary.objects.filter(
                benefit_plan_id=benefit_plan.id,
                status=BeneficiaryStatus.ACTIVE,
                is_deleted=False,
            ).count()
            - len(candidates)
        )
        self.stdout.write(f"Bénéficiaires actifs après opération (attendu ~15299): {active_after}")

        if not execute:
            self.stdout.write(
                self.style.WARNING(
                    "Mode simulation (dry-run). Relancer avec --execute pour appliquer."
                )
            )
            return

        if fast:
            deleted_benef, deleted_ind = self._apply_deletions_fast(
                user, candidates, purge_individuals, batch_size
            )
        else:
            deleted_benef, deleted_ind = self._apply_deletions(
                user, candidates, purge_individuals, batch_size
            )
        self.stdout.write(
            self.style.SUCCESS(
                f"Terminé: {deleted_benef} bénéficiaire(s) supprimé(s)"
                + (f", {deleted_ind} individu(s) purgé(s)" if purge_individuals else "")
            )
        )

    def _parse_cutoff(self, value):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                parsed = datetime.strptime(value.strip(), fmt)
                return parsed
            except ValueError:
                continue
        raise ValueError(f"Date invalide: {value}")

    def _normalize_dt(self, dt):
        if dt is None:
            return None
        if timezone.is_aware(dt):
            return timezone.make_naive(dt, timezone.utc)
        return dt

    def _menage_code(self, beneficiary):
        json_ext = beneficiary.json_ext or {}
        individual_ext = beneficiary.individual.json_ext if beneficiary.individual else {}
        return (json_ext.get("code_menage") or individual_ext.get("code_menage") or "").strip()

    def _resolve_second_lot_candidates(self, benefit_plan_id, cutoff):
        """
        Cible le bénéficiaire le plus récent par ménage lorsqu'il y en a exactement 2,
        et vérifie qu'il appartient au 2e lot (date_created >= cutoff).
        """
        active = (
            Beneficiary.objects.filter(
                benefit_plan_id=benefit_plan_id,
                status=BeneficiaryStatus.ACTIVE,
                is_deleted=False,
            )
            .select_related("individual")
            .only(
                "id",
                "individual_id",
                "date_created",
                "json_ext",
                "individual__json_ext",
            )
        )

        by_menage = defaultdict(list)
        no_menage = []
        for beneficiary in active.iterator(chunk_size=3000):
            code = self._menage_code(beneficiary)
            if not code:
                no_menage.append(beneficiary.id)
                continue
            by_menage[code].append(beneficiary)

        if no_menage:
            self.stdout.write(
                self.style.WARNING(f"{len(no_menage)} bénéficiaire(s) sans code_menage (ignorés).")
            )

        candidates = []
        skipped_menage = Counter()
        for code, rows in by_menage.items():
            if len(rows) != 2:
                skipped_menage[f"count_{len(rows)}"] += 1
                continue
            rows_sorted = sorted(rows, key=lambda b: b.date_created or datetime.min)
            second = rows_sorted[1]
            second_created = self._normalize_dt(second.date_created)
            if second_created and cutoff and second_created >= cutoff:
                candidates.append(second)
            else:
                skipped_menage["cutoff_mismatch"] += 1

        if skipped_menage:
            self.stdout.write(f"Ménages ignorés (répartition): {dict(skipped_menage)}")

        return candidates

    def _find_blocked(self, candidates):
        blocked = []
        individual_ids = [b.individual_id for b in candidates]
        benef_ids = [b.id for b in candidates]

        payroll_usage = set(
            BenefitConsumption.objects.filter(
                individual_id__in=individual_ids, is_deleted=False
            ).values_list("individual_id", flat=True)
        )
        payroll_link = set(
            PayrollBenefitConsumption.objects.filter(
                benefit_id__in=benef_ids, is_deleted=False
            ).values_list("benefit_id", flat=True)
        )
        other_plan = set(
            Beneficiary.objects.filter(
                individual_id__in=individual_ids, is_deleted=False
            )
            .exclude(id__in=benef_ids)
            .values_list("individual_id", flat=True)
        )

        for beneficiary in candidates:
            reasons = []
            if beneficiary.individual_id in payroll_usage:
                reasons.append("BenefitConsumption")
            if beneficiary.id in payroll_link:
                reasons.append("PayrollBenefitConsumption")
            if beneficiary.individual_id in other_plan:
                reasons.append("autre régime")
            if reasons:
                blocked.append(f"{beneficiary.id} ({', '.join(reasons)})")
        return blocked

    def _apply_deletions(self, user, candidates, purge_individuals, batch_size):
        from individual.models import Individual

        deleted_benef = 0
        deleted_ind = 0
        username = user.username

        for offset in range(0, len(candidates), batch_size):
            chunk = candidates[offset : offset + batch_size]
            with transaction.atomic():
                for beneficiary in chunk:
                    beneficiary.refresh_from_db()
                    if beneficiary.is_deleted:
                        continue
                    beneficiary.delete(user=user, username=username)
                    deleted_benef += 1

                    if purge_individuals:
                        individual = Individual.objects.filter(
                            id=beneficiary.individual_id, is_deleted=False
                        ).first()
                        if not individual:
                            continue
                        still_linked = Beneficiary.objects.filter(
                            individual_id=individual.id, is_deleted=False
                        ).exists()
                        if still_linked:
                            continue
                        has_consumption = BenefitConsumption.objects.filter(
                            individual_id=individual.id, is_deleted=False
                        ).exists()
                        if has_consumption:
                            continue
                        individual.delete(user=user, username=username)
                        deleted_ind += 1

            self.stdout.write(f"  Lot {offset // batch_size + 1}: {len(chunk)} traité(s)")

        return deleted_benef, deleted_ind

    def _apply_deletions_fast(self, user, candidates, purge_individuals, batch_size):
        """Soft-delete par lots (update) — adapté aux gros volumes."""
        from individual.models import Individual

        now = timezone.now()
        deleted_benef = 0
        deleted_ind = 0
        candidate_ids = [b.id for b in candidates]
        individual_ids = list({b.individual_id for b in candidates})

        for offset in range(0, len(candidate_ids), batch_size):
            chunk_ids = candidate_ids[offset : offset + batch_size]
            with transaction.atomic():
                updated = Beneficiary.objects.filter(
                    id__in=chunk_ids, is_deleted=False
                ).update(
                    is_deleted=True,
                    date_updated=now,
                    user_updated=user,
                    version=F("version") + 1,
                )
                deleted_benef += updated
            self.stdout.write(f"  Bénéficiaires lot {offset // batch_size + 1}: {updated}")

        if purge_individuals:
            remaining_links = set(
                Beneficiary.objects.filter(
                    individual_id__in=individual_ids, is_deleted=False
                ).values_list("individual_id", flat=True)
            )
            consumption_links = set(
                BenefitConsumption.objects.filter(
                    individual_id__in=individual_ids, is_deleted=False
                ).values_list("individual_id", flat=True)
            )
            purge_ids = [
                iid
                for iid in individual_ids
                if iid not in remaining_links and iid not in consumption_links
            ]
            for offset in range(0, len(purge_ids), batch_size):
                chunk_ids = purge_ids[offset : offset + batch_size]
                with transaction.atomic():
                    updated = Individual.objects.filter(
                        id__in=chunk_ids, is_deleted=False
                    ).update(
                        is_deleted=True,
                        date_updated=now,
                        user_updated=user,
                        version=F("version") + 1,
                    )
                    deleted_ind += updated
                self.stdout.write(f"  Individus lot {offset // batch_size + 1}: {updated}")

        return deleted_benef, deleted_ind
