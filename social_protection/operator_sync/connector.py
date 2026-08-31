import logging

import requests

from payroll.payment_gateway.payment_gateway_config import PaymentGatewayConfig

logger = logging.getLogger(__name__)


class BeneficiaryOperatorConnector:
    """
    Client HTTP vers l'opérateur de paiement pour la sync bénéficiaires
    (POST ENDPOINT_BENEFICIARY, mêmes credentials que le paiement).
    """

    def __init__(self):
        self.config = PaymentGatewayConfig()
        self.session = requests.Session()
        self.session.headers.update(self.config.get_headers())

    def _url(self):
        base = (self.config.gateway_base_url or "").rstrip("/")
        endpoint = (self.config.endpoint_beneficiary or "Beneficiary/sync").lstrip("/")
        if not base:
            raise ValueError("PAYMENT_GATEWAY_BASE_URL is not configured")
        return f"{base}/{endpoint}"

    def sync(self, payload: dict):
        url = self._url()
        logger.info(
            "[OperatorSync] POST %s operation=%s requestId=%s",
            url,
            payload.get("operation"),
            payload.get("requestId"),
        )
        response = self.session.post(
            url,
            json=payload,
            timeout=self.config.timeout or 30,
        )
        return response
