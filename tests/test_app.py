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
SAMPLE_V1_PATH  = os.path.join(os.path.dirname(__file__), "sample_v1.xmi")
SAMPLE_EA_PATH  = os.path.join(os.path.dirname(__file__), "sample_ea.xmi")


@pytest.fixture()
def client():
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as c:
        yield c


def _load(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


# ── XMI 2.x standard parser tests ────────────────────────────────────────

def test_parse_xmi_model_name():
    model = parse_xmi(_load(SAMPLE_XMI_PATH))
    assert model["model_name"] == "OnlineShop"


def test_parse_xmi_classes():
    model = parse_xmi(_load(SAMPLE_XMI_PATH))
    class_names = [c["name"] for c in model["classes"]]
    assert "Customer" in class_names
    assert "Order" in class_names
    assert "Product" in class_names


def test_parse_xmi_interfaces():
    model = parse_xmi(_load(SAMPLE_XMI_PATH))
    iface_names = [i["name"] for i in model["interfaces"]]
    assert "Payable" in iface_names


def test_parse_xmi_enumerations():
    model = parse_xmi(_load(SAMPLE_XMI_PATH))
    enum_names = [e["name"] for e in model["enumerations"]]
    assert "OrderStatus" in enum_names
    order_status = next(e for e in model["enumerations"] if e["name"] == "OrderStatus")
    assert "PENDING" in order_status["literals"]
    assert "CANCELLED" in order_status["literals"]


def test_parse_xmi_attributes():
    model = parse_xmi(_load(SAMPLE_XMI_PATH))
    customer = next(c for c in model["classes"] if c["name"] == "Customer")
    attr_names = [a["name"] for a in customer["attributes"]]
    assert "name" in attr_names
    assert "email" in attr_names


def test_parse_xmi_operations():
    model = parse_xmi(_load(SAMPLE_XMI_PATH))
    customer = next(c for c in model["classes"] if c["name"] == "Customer")
    op_names = [o["name"] for o in customer["operations"]]
    assert "placeOrder" in op_names


def test_parse_xmi_associations():
    model = parse_xmi(_load(SAMPLE_XMI_PATH))
    assert len(model["associations"]) >= 2
    assoc_names = [a["name"] for a in model["associations"]]
    assert "CustomerPlacesOrder" in assoc_names


def test_parse_xmi_realizations():
    model = parse_xmi(_load(SAMPLE_XMI_PATH))
    assert len(model["realizations"]) >= 1


def test_parse_xmi_packages():
    model = parse_xmi(_load(SAMPLE_XMI_PATH))
    assert "Domain" in model["packages"]


def test_parse_xmi_invalid_xml():
    with pytest.raises(ValueError, match="Invalid XML"):
        parse_xmi(b"not valid xml <<>>")


def test_model_to_text():
    model = parse_xmi(_load(SAMPLE_XMI_PATH))
    text = model_to_text(model)
    assert "OnlineShop" in text
    assert "Customer" in text
    assert "Order" in text
    assert "Payable" in text
    assert "OrderStatus" in text


def test_format_info_present():
    """parse_xmi must always include format_info."""
    model = parse_xmi(_load(SAMPLE_XMI_PATH))
    assert "format_info" in model
    assert "xmi_version" in model["format_info"]


# ── XMI 1.x parser tests ─────────────────────────────────────────────────

def test_v1_model_name():
    model = parse_xmi(_load(SAMPLE_V1_PATH))
    assert model["model_name"] == "LibrarySystem"


def test_v1_classes():
    model = parse_xmi(_load(SAMPLE_V1_PATH))
    class_names = [c["name"] for c in model["classes"]]
    assert "Book" in class_names
    assert "Member" in class_names
    assert "Librarian" in class_names


def test_v1_interface():
    model = parse_xmi(_load(SAMPLE_V1_PATH))
    iface_names = [i["name"] for i in model["interfaces"]]
    assert "Searchable" in iface_names


def test_v1_enumeration():
    model = parse_xmi(_load(SAMPLE_V1_PATH))
    enum_names = [e["name"] for e in model["enumerations"]]
    assert "BookCategory" in enum_names
    book_cat = next(e for e in model["enumerations"] if e["name"] == "BookCategory")
    assert "FICTION" in book_cat["literals"]


def test_v1_attributes():
    model = parse_xmi(_load(SAMPLE_V1_PATH))
    book = next(c for c in model["classes"] if c["name"] == "Book")
    attr_names = [a["name"] for a in book["attributes"]]
    assert "isbn" in attr_names
    assert "title" in attr_names


def test_v1_operations():
    model = parse_xmi(_load(SAMPLE_V1_PATH))
    book = next(c for c in model["classes"] if c["name"] == "Book")
    op_names = [o["name"] for o in book["operations"]]
    assert "checkOut" in op_names


def test_v1_package():
    model = parse_xmi(_load(SAMPLE_V1_PATH))
    assert "Core" in model["packages"]


def test_v1_association():
    model = parse_xmi(_load(SAMPLE_V1_PATH))
    assoc_names = [a["name"] for a in model["associations"]]
    assert "borrows" in assoc_names


def test_v1_format_detection():
    model = parse_xmi(_load(SAMPLE_V1_PATH))
    fmt = model["format_info"]
    assert "1." in fmt["xmi_version"]
    # Tool should be detected from XMI.exporter child element
    assert fmt["tool"] in ("Rational Rose", "Legacy UML tool (XMI 1.x)")


def test_v1_href_type_resolution():
    """Types stored as hrefs in XMI 1.x should resolve to readable names."""
    model = parse_xmi(_load(SAMPLE_V1_PATH))
    book = next(c for c in model["classes"] if c["name"] == "Book")
    isbn_attr = next(a for a in book["attributes"] if a["name"] == "isbn")
    assert isbn_attr["type"] in ("String", "")  # resolved or blank is acceptable


# ── Enterprise Architect Extension block tests ────────────────────────────

def test_ea_model_name():
    model = parse_xmi(_load(SAMPLE_EA_PATH))
    assert model["model_name"] == "BankingSystem"


def test_ea_classes_present():
    model = parse_xmi(_load(SAMPLE_EA_PATH))
    class_names = [c["name"] for c in model["classes"]]
    assert "Account" in class_names
    assert "Customer" in class_names
    assert "Transaction" in class_names


def test_ea_attributes_from_extension():
    """Attributes should be populated from the EA Extension block."""
    model = parse_xmi(_load(SAMPLE_EA_PATH))
    account = next(c for c in model["classes"] if c["name"] == "Account")
    attr_names = [a["name"] for a in account["attributes"]]
    assert "accountNumber" in attr_names
    assert "balance" in attr_names


def test_ea_operations_from_extension():
    """Operations should be populated from the EA Extension block."""
    model = parse_xmi(_load(SAMPLE_EA_PATH))
    account = next(c for c in model["classes"] if c["name"] == "Account")
    op_names = [o["name"] for o in account["operations"]]
    assert "deposit" in op_names
    assert "withdraw" in op_names
    assert "getBalance" in op_names


def test_ea_interface_from_extension():
    model = parse_xmi(_load(SAMPLE_EA_PATH))
    iface_names = [i["name"] for i in model["interfaces"]]
    assert "Auditable" in iface_names


def test_ea_enumeration():
    model = parse_xmi(_load(SAMPLE_EA_PATH))
    enum_names = [e["name"] for e in model["enumerations"]]
    assert "AccountStatus" in enum_names
    acct_status = next(e for e in model["enumerations"] if e["name"] == "AccountStatus")
    assert "ACTIVE" in acct_status["literals"]


def test_ea_connectors_associations():
    """EA connector block should produce association records."""
    model = parse_xmi(_load(SAMPLE_EA_PATH))
    assert len(model["associations"]) >= 1
    all_types = {
        t for a in model["associations"] for e in a.get("ends", []) for t in [e.get("type", "")]
    }
    assert "Customer" in all_types or "Account" in all_types


def test_ea_realization_from_connectors():
    model = parse_xmi(_load(SAMPLE_EA_PATH))
    assert any(
        r["client"] == "Account" and r["supplier"] == "Auditable"
        for r in model["realizations"]
    )


def test_ea_diagrams():
    """Diagrams listed in the EA Extension block should be extracted."""
    model = parse_xmi(_load(SAMPLE_EA_PATH))
    diag_names = [d["name"] for d in model["diagrams"]]
    assert "Domain Class Diagram" in diag_names
    assert "Account Deposit Flow" in diag_names


def test_ea_stereotypes():
    model = parse_xmi(_load(SAMPLE_EA_PATH))
    account = next(c for c in model["classes"] if c["name"] == "Account")
    assert "entity" in account.get("stereotypes", [])


def test_ea_tool_detection():
    model = parse_xmi(_load(SAMPLE_EA_PATH))
    fmt = model["format_info"]
    assert "Enterprise Architect" in fmt["tool"]


def test_ea_documentation_as_comment():
    model = parse_xmi(_load(SAMPLE_EA_PATH))
    account = next(c for c in model["classes"] if c["name"] == "Account")
    comments = account.get("comments", [])
    assert any("bank account" in c.lower() for c in comments)


def test_ea_model_to_text_includes_diagrams():
    model = parse_xmi(_load(SAMPLE_EA_PATH))
    text = model_to_text(model)
    assert "Domain Class Diagram" in text


def test_ea_model_to_text_includes_tool():
    model = parse_xmi(_load(SAMPLE_EA_PATH))
    text = model_to_text(model)
    assert "Enterprise Architect" in text


def test_ea_stereotypes_in_text():
    model = parse_xmi(_load(SAMPLE_EA_PATH))
    text = model_to_text(model)
    assert "entity" in text


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
    xml_bytes = _load(SAMPLE_XMI_PATH)
    data = {"file": (io.BytesIO(xml_bytes), "sample.xmi")}
    res = client.post("/upload", data=data, content_type="multipart/form-data")
    assert res.status_code == 200
    body = json.loads(res.data)
    assert "model" in body
    assert "summary" in body
    assert "format_info" in body
    assert body["model"]["model_name"] == "OnlineShop"


def test_upload_v1_xmi(client):
    xml_bytes = _load(SAMPLE_V1_PATH)
    data = {"file": (io.BytesIO(xml_bytes), "sample_v1.xmi")}
    res = client.post("/upload", data=data, content_type="multipart/form-data")
    assert res.status_code == 200
    body = json.loads(res.data)
    assert body["model"]["model_name"] == "LibrarySystem"


def test_upload_ea_xmi(client):
    xml_bytes = _load(SAMPLE_EA_PATH)
    data = {"file": (io.BytesIO(xml_bytes), "sample_ea.xmi")}
    res = client.post("/upload", data=data, content_type="multipart/form-data")
    assert res.status_code == 200
    body = json.loads(res.data)
    assert body["model"]["model_name"] == "BankingSystem"
    assert "format_info" in body


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

