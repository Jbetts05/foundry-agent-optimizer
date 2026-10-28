import argparse

import pytest

from scripts.verify_public_demo import deployment_url


@pytest.mark.parametrize("value", ["https://example.com", "http://localhost:8000/"])
def test_deployment_origin_is_explicit_and_normalized(value):
    assert deployment_url(value) == value.rstrip("/")


@pytest.mark.parametrize("value", [
    "",
    "example.com",
    "file:///example",
    "https://user:password@example.com",
    "https://example.com/path",
    "https://example.com?token=secret",
    "https://example.com#fragment",
])
def test_deployment_origin_rejects_non_origins_and_credentials(value):
    with pytest.raises(argparse.ArgumentTypeError):
        deployment_url(value)
