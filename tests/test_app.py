"""
Tests for app.py - XMI parsing and Flask endpoints.
"""

import io
import json
import pytest

# Make sure the app module is importable from the project root
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import app as flask_app, parse_xmi, model_to_text

SAMPLE_XMI_PATH = os.path.join(os.path.dirname(__file__), "sample.xmi")


@pytest.fixture()
def client():
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as c:
        yield c


# ── XMI Parser tests ──────────────────────────────────────────────────────

def load_sample() -> bytes:
    with open(SAMPLE_XMI_PATH, "rb") as f:
        return f.read()


def test_parse_xmi_model_name():
    model = parse_xmi(load_sample())
    assert model["model_name"] == "OnlineShop"


def test_parse_xmi_classes():
    model = parse_xmi(load_sample())
    class_names = [c["name"] for c in model["classes"]]
    assert "Customer" in class_names
    assert "Order" in class_names
    assert "Product" in class_names


def test_parse_xmi_interfaces():
    model = parse_xmi(load_sample())
    iface_names = [i["name"] for i in model["interfaces"]]
    assert "Payable" in iface_names


def test_parse_xmi_enumerations():
    model = parse_xmi(load_sample())
    enum_names = [e["name"] for e in model["enumerations"]]
    assert "OrderStatus" in enum_names
    order_status = next(e for e in model["enumerations"] if e["name"] == "OrderStatus")
    assert "PENDING" in order_status["literals"]
    assert "CANCELLED" in order_status["literals"]


def test_parse_xmi_attributes():
    model = parse_xmi(load_sample())
    customer = next(c for c in model["classes"] if c["name"] == "Customer")
    attr_names = [a["name"] for a in customer["attributes"]]
    assert "name" in attr_names
    assert "email" in attr_names


def test_parse_xmi_operations():
    model = parse_xmi(load_sample())
    customer = next(c for c in model["classes"] if c["name"] == "Customer")
    op_names = [o["name"] for o in customer["operations"]]
    assert "placeOrder" in op_names


def test_parse_xmi_associations():
    model = parse_xmi(load_sample())
    assert len(model["associations"]) >= 2
    assoc_names = [a["name"] for a in model["associations"]]
    assert "CustomerPlacesOrder" in assoc_names


def test_parse_xmi_realizations():
    model = parse_xmi(load_sample())
    assert len(model["realizations"]) >= 1


def test_parse_xmi_packages():
    model = parse_xmi(load_sample())
    assert "Domain" in model["packages"]


def test_parse_xmi_invalid_xml():
    with pytest.raises(ValueError, match="Invalid XML"):
        parse_xmi(b"not valid xml <<>>")


def test_model_to_text():
    model = parse_xmi(load_sample())
    text = model_to_text(model)
    assert "OnlineShop" in text
    assert "Customer" in text
    assert "Order" in text
    assert "Payable" in text
    assert "OrderStatus" in text


# ── Flask endpoint tests ──────────────────────────────────────────────────

def test_index_route(client):
    res = client.get("/")
    assert res.status_code == 200
    assert b"XMI" in res.data


def test_upload_no_file(client):
    res = client.post("/upload")
    assert res.status_code == 400
    data = json.loads(res.data)
    assert "error" in data


def test_upload_wrong_extension(client):
    data = {"file": (io.BytesIO(b"<xml/>"), "model.txt")}
    res = client.post("/upload", data=data, content_type="multipart/form-data")
    assert res.status_code == 400
    body = json.loads(res.data)
    assert "error" in body


def test_upload_valid_xmi(client):
    xml_bytes = load_sample()
    data = {"file": (io.BytesIO(xml_bytes), "sample.xmi")}
    res = client.post("/upload", data=data, content_type="multipart/form-data")
    assert res.status_code == 200
    body = json.loads(res.data)
    assert "model" in body
    assert "summary" in body
    assert body["model"]["model_name"] == "OnlineShop"


def test_upload_invalid_xml_content(client):
    data = {"file": (io.BytesIO(b"not valid xml"), "bad.xmi")}
    res = client.post("/upload", data=data, content_type="multipart/form-data")
    assert res.status_code == 422
    body = json.loads(res.data)
    assert "error" in body


def test_review_no_api_key(client):
    payload = {"summary": "Some summary", "question": "Review this"}
    res = client.post("/review", data=json.dumps(payload),
                      content_type="application/json")
    assert res.status_code == 400
    body = json.loads(res.data)
    assert "error" in body


def test_review_no_summary(client):
    payload = {"api_key": "sk-test", "question": "Review this"}
    res = client.post("/review", data=json.dumps(payload),
                      content_type="application/json")
    assert res.status_code == 400
    body = json.loads(res.data)
    assert "error" in body
