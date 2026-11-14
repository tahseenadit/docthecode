from app.routers.setup import credentials_from_conversion

BASE = {"id": 123, "slug": "docthecode-x1", "client_id": "Iv1.abc", "client_secret": "cs", "pem": "-----BEGIN RSA PRIVATE KEY-----\n..."}


def test_conversion_keeps_githubs_webhook_secret():
    creds = credentials_from_conversion({**BASE, "webhook_secret": "from-github"})
    assert creds["webhook_secret"] == "from-github"
    assert creds["app_id"] == "123" and creds["private_key"].startswith("-----BEGIN")


def test_conversion_without_webhook_gets_a_generated_secret():
    # Apps created with "GitHub can reach this address" unticked have no webhook, and GitHub returns null.
    for missing in ({**BASE, "webhook_secret": None}, dict(BASE)):
        secret = credentials_from_conversion(missing)["webhook_secret"]
        assert isinstance(secret, str) and len(secret) == 64
