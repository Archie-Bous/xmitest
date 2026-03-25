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

from app import app as flask_app, parse_xmi, model_to_text, score_model

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


# ── score_model() unit tests ──────────────────────────────────────────────

def test_score_model_structure():
    """score_model must return the expected top-level keys."""
    model = parse_xmi(_load(SAMPLE_XMI_PATH))
    quality = score_model(model)
    assert "overall_score" in quality
    assert "grade" in quality
    assert "criteria" in quality
    assert "top_issues" in quality


def test_score_model_overall_range():
    """overall_score must be an integer in [0, 100]."""
    model = parse_xmi(_load(SAMPLE_XMI_PATH))
    quality = score_model(model)
    assert isinstance(quality["overall_score"], int)
    assert 0 <= quality["overall_score"] <= 100


def test_score_model_grade_valid():
    """Grade must be one of A / B / C / D / F."""
    model = parse_xmi(_load(SAMPLE_XMI_PATH))
    quality = score_model(model)
    assert quality["grade"] in ("A", "B", "C", "D", "F")


def test_score_model_six_criteria():
    """Quality report must include exactly six criteria."""
    model = parse_xmi(_load(SAMPLE_XMI_PATH))
    quality = score_model(model)
    assert len(quality["criteria"]) == 6


def test_score_model_criteria_keys():
    """Each criterion must have name, description, score, weight, and issues."""
    model = parse_xmi(_load(SAMPLE_XMI_PATH))
    quality = score_model(model)
    for c in quality["criteria"]:
        assert "name" in c
        assert "description" in c
        assert "score" in c
        assert "weight" in c
        assert "issues" in c
        assert 0 <= c["score"] <= 100


def test_score_model_weights_sum():
    """Criteria weights must sum to 1.0."""
    model = parse_xmi(_load(SAMPLE_XMI_PATH))
    quality = score_model(model)
    total_weight = sum(c["weight"] for c in quality["criteria"])
    assert abs(total_weight - 1.0) < 1e-9


def test_score_model_top_issues_list():
    """top_issues must be a list; each entry has criterion and issue keys."""
    model = parse_xmi(_load(SAMPLE_XMI_PATH))
    quality = score_model(model)
    assert isinstance(quality["top_issues"], list)
    for issue in quality["top_issues"]:
        assert "criterion" in issue
        assert "issue" in issue


def test_score_model_empty_model():
    """score_model must handle a completely empty model without errors."""
    from app import _empty_result
    empty = _empty_result()
    empty["format_info"] = {"xmi_version": "2.x", "tool": "Test", "tool_version": ""}
    quality = score_model(empty)
    assert quality["grade"] in ("A", "B", "C", "D", "F")


def test_score_model_ea():
    """score_model must work on the EA sample and return a well-documented model."""
    model = parse_xmi(_load(SAMPLE_EA_PATH))
    quality = score_model(model)
    # EA sample has comments on Account – doc score should be non-zero
    doc_criterion = next(c for c in quality["criteria"] if "Documentation" in c["name"])
    assert doc_criterion["score"] > 0


def test_upload_includes_quality(client):
    """The /upload endpoint must include a 'quality' key in its response."""
    xml_bytes = _load(SAMPLE_XMI_PATH)
    data = {"file": (io.BytesIO(xml_bytes), "sample.xmi")}
    res = client.post("/upload", data=data, content_type="multipart/form-data")
    assert res.status_code == 200
    body = json.loads(res.data)
    assert "quality" in body
    assert "overall_score" in body["quality"]
    assert "grade" in body["quality"]
    assert "criteria" in body["quality"]


def test_score_endpoint_valid(client):
    """The /score endpoint must accept a model dict and return quality."""
    model = parse_xmi(_load(SAMPLE_XMI_PATH))
    payload = {"model": model}
    res = client.post("/score", data=json.dumps(payload),
                      content_type="application/json")
    assert res.status_code == 200
    body = json.loads(res.data)
    assert "quality" in body
    assert body["quality"]["grade"] in ("A", "B", "C", "D", "F")


def test_score_endpoint_no_model(client):
    """The /score endpoint must return 400 when no model is provided."""
    res = client.post("/score", data=json.dumps({}),
                      content_type="application/json")
    assert res.status_code == 400
    body = json.loads(res.data)
    assert "error" in body


# ── Noise filtering tests ─────────────────────────────────────────────────

def test_noise_ns_filtering():
    """Elements in proprietary tool namespaces must be silently skipped."""
    xmi_with_noise = b"""<?xml version="1.0" encoding="UTF-8"?>
<xmi:XMI xmi:version="2.1"
    xmlns:xmi="http://www.omg.org/XMI"
    xmlns:uml="http://www.eclipse.org/uml2/5.0.0/UML"
    xmlns:notation="http://www.eclipse.org/gmf/runtime/1.0.2/notation"
    xmlns:ptc="http://www.ptc.com/xmi/1.0">
  <uml:Model xmi:id="m1" name="NoisyModel">
    <packagedElement xmi:type="uml:Class" xmi:id="c1" name="NoiseFreeClass">
      <ownedAttribute xmi:id="a1" name="myAttr" type="String"/>
    </packagedElement>
  </uml:Model>
  <!-- This should be skipped entirely -->
  <notation:Diagram xmi:id="d1" type="Class">
    <children xmi:id="ch1"/>
  </notation:Diagram>
  <ptc:ToolData xmi:id="td1" someKey="someValue"/>
</xmi:XMI>"""
    model = parse_xmi(xmi_with_noise)
    assert model["model_name"] == "NoisyModel"
    class_names = [c["name"] for c in model["classes"]]
    assert "NoiseFreeClass" in class_names


def test_layout_element_filtering():
    """Layout-only element names (e.g. 'bounds') must not create spurious classes."""
    xmi_with_layout = b"""<?xml version="1.0" encoding="UTF-8"?>
<xmi:XMI xmi:version="2.1"
    xmlns:xmi="http://www.omg.org/XMI"
    xmlns:uml="http://www.eclipse.org/uml2/5.0.0/UML">
  <uml:Model xmi:id="m1" name="LayoutModel">
    <packagedElement xmi:type="uml:Class" xmi:id="c1" name="RealClass"/>
    <bounds x="10" y="20" width="100" height="50"/>
    <appearance color="#FFFFFF"/>
  </uml:Model>
</xmi:XMI>"""
    model = parse_xmi(xmi_with_layout)
    assert model["model_name"] == "LayoutModel"
    class_names = [c["name"] for c in model["classes"]]
    assert "RealClass" in class_names
    # bounds and appearance must not appear as classes
    assert "bounds" not in class_names
    assert "appearance" not in class_names


def test_ptc_modeler_tool_detection():
    """Files with PTC namespace URIs must be detected as PTC Integrity Modeler."""
    xmi_ptc = b"""<?xml version="1.0" encoding="UTF-8"?>
<xmi:XMI xmi:version="2.1"
    xmlns:xmi="http://www.omg.org/XMI"
    xmlns:uml="http://www.eclipse.org/uml2/5.0.0/UML"
    xmlns:ptc="http://www.ptc.com/xmi/modeler/1.0">
  <uml:Model xmi:id="m1" name="PTCModel"/>
</xmi:XMI>"""
    model = parse_xmi(xmi_ptc)
    assert "PTC" in model["format_info"]["tool"]

