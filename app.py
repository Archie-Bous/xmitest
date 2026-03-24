"""
XMI UML Review Tool - Flask Backend

A web-based tool for uploading XMI files from UML tools and using
LLM integration to review the underlying UML model.
"""

import os
import re

from flask import Flask, request, jsonify, render_template
from lxml import etree
from openai import OpenAI, APIError, APIConnectionError, RateLimitError, AuthenticationError
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB upload limit

DEFAULT_REVIEW_QUESTION = "Please provide a comprehensive review of this UML model."

# ---------------------------------------------------------------------------
# XMI namespaces used by common UML tools
# ---------------------------------------------------------------------------
XMI_NS = {
    "xmi": "http://www.omg.org/XMI",
    "uml": "http://www.eclipse.org/uml2/5.0.0/UML",
    "uml2": "http://www.omg.org/spec/UML/20110701",
    "uml3": "http://www.omg.org/spec/UML/20131001",
}


# ---------------------------------------------------------------------------
# XMI Parsing
# ---------------------------------------------------------------------------

def _tag_local(element):
    """Return the local (namespace-stripped) tag name."""
    tag = element.tag
    return re.sub(r"\{[^}]+\}", "", tag)


def _xmi_type(element):
    """Return the xmi:type attribute value (local part only)."""
    for ns in XMI_NS.values():
        val = element.get(f"{{{ns}}}type")
        if val:
            return val.split(":")[-1]
    return element.get("xmi:type", "").split(":")[-1]


def _xmi_id(element):
    """Return the xmi:id attribute."""
    for ns in XMI_NS.values():
        val = element.get(f"{{{ns}}}id")
        if val:
            return val
    return element.get("xmi:id", "")


def _collect_tagged_values(element):
    """Collect any tagged values / stereotypes applied to an element."""
    tags = []
    for child in element:
        local = _tag_local(child)
        if local in ("TaggedValue", "appliedStereotype", "ownedComment"):
            value = child.get("value") or child.get("name") or child.get("body", "")
            if value:
                tags.append(value)
    return tags


def parse_xmi(xml_bytes: bytes) -> dict:
    """
    Parse an XMI byte string and return a structured dict describing the
    UML model.  Handles XMI files from Enterprise Architect, MagicDraw,
    Papyrus, Visual Paradigm, Sparx, and similar tools.
    """
    try:
        root = etree.fromstring(xml_bytes)
    except etree.XMLSyntaxError as exc:
        raise ValueError(f"Invalid XML/XMI file: {exc}") from exc

    result = {
        "model_name": "Unknown Model",
        "packages": [],
        "classes": [],
        "interfaces": [],
        "enumerations": [],
        "actors": [],
        "use_cases": [],
        "components": [],
        "associations": [],
        "dependencies": [],
        "realizations": [],
        "generalizations": [],
        "notes": [],
    }

    # Build an id → name lookup table for resolving references
    id_name: dict[str, str] = {}

    def index_ids(el):
        xid = _xmi_id(el)
        name = el.get("name", "")
        if xid and name:
            id_name[xid] = name
        for child in el:
            index_ids(child)

    index_ids(root)

    def resolve(ref: str) -> str:
        return id_name.get(ref, ref)

    def parse_owned_attribute(attr_el):
        name = attr_el.get("name", "")
        type_ref = attr_el.get("type", "")
        visibility = attr_el.get("visibility", "public")
        is_static = attr_el.get("isStatic", "false") == "true"
        multiplicity_low = attr_el.get("lower", "1")
        multiplicity_high = attr_el.get("upper", "1")

        # Inline type element
        type_name = resolve(type_ref) if type_ref else ""
        type_el = attr_el.find("type")
        if type_el is not None:
            href = type_el.get("{http://www.w3.org/1999/xlink}href", type_el.get("href", ""))
            if href:
                type_name = href.split("#")[-1].split("/")[-1]

        return {
            "name": name,
            "type": type_name,
            "visibility": visibility,
            "isStatic": is_static,
            "multiplicity": f"{multiplicity_low}..{multiplicity_high}",
        }

    def parse_owned_operation(op_el):
        name = op_el.get("name", "")
        visibility = op_el.get("visibility", "public")
        is_static = op_el.get("isStatic", "false") == "true"
        params = []
        return_type = ""
        for param in op_el:
            if _tag_local(param) == "ownedParameter":
                direction = param.get("direction", "in")
                if direction == "return":
                    type_ref = param.get("type", "")
                    return_type = resolve(type_ref) if type_ref else "void"
                else:
                    params.append({
                        "name": param.get("name", ""),
                        "type": resolve(param.get("type", "")),
                        "direction": direction,
                    })
        return {
            "name": name,
            "visibility": visibility,
            "isStatic": is_static,
            "parameters": params,
            "returnType": return_type or "void",
        }

    def process_classifier(el, category):
        entry = {
            "name": el.get("name", ""),
            "visibility": el.get("visibility", "public"),
            "isAbstract": el.get("isAbstract", "false") == "true",
            "attributes": [],
            "operations": [],
            "generalizations": [],
            "comments": [],
        }
        for child in el:
            local = _tag_local(child)
            if local == "ownedAttribute":
                entry["attributes"].append(parse_owned_attribute(child))
            elif local == "ownedOperation":
                entry["operations"].append(parse_owned_operation(child))
            elif local == "generalization":
                general_ref = child.get("general", "")
                if general_ref:
                    entry["generalizations"].append(resolve(general_ref))
            elif local == "ownedComment":
                body = child.get("body", "")
                if body:
                    entry["comments"].append(body)
        result[category].append(entry)

    def process_enumeration(el):
        entry = {
            "name": el.get("name", ""),
            "literals": [],
        }
        for child in el:
            if _tag_local(child) == "ownedLiteral":
                entry["literals"].append(child.get("name", ""))
        result["enumerations"].append(entry)

    def process_association(el):
        ends = []
        for child in el:
            if _tag_local(child) in ("ownedEnd", "memberEnd", "navigableOwnedEnd"):
                type_ref = child.get("type", "")
                ends.append({
                    "name": child.get("name", ""),
                    "type": resolve(type_ref),
                    "aggregation": child.get("aggregation", "none"),
                    "multiplicity": f"{child.get('lower', '0')}..{child.get('upper', '*')}",
                    "navigable": _tag_local(child) == "navigableOwnedEnd",
                })
        # Resolve member-end references (some tools use refs not child elements)
        member_ends_attr = el.get("memberEnd", "")
        if member_ends_attr and not ends:
            for ref in member_ends_attr.split():
                ends.append({"type": resolve(ref), "name": "", "aggregation": "none"})
        result["associations"].append({
            "name": el.get("name", ""),
            "ends": ends,
        })

    def process_dependency(el, kind="dependency"):
        client = resolve(el.get("client", ""))
        supplier = resolve(el.get("supplier", ""))
        result["dependencies"].append({
            "kind": kind,
            "name": el.get("name", ""),
            "client": client,
            "supplier": supplier,
        })

    def process_realization(el):
        client = resolve(el.get("client", ""))
        supplier = resolve(el.get("supplier", ""))
        result["realizations"].append({
            "name": el.get("name", ""),
            "client": client,
            "supplier": supplier,
        })

    TYPE_DISPATCH = {
        "Class": lambda el: process_classifier(el, "classes"),
        "Interface": lambda el: process_classifier(el, "interfaces"),
        "Actor": lambda el: process_classifier(el, "actors"),
        "UseCase": lambda el: process_classifier(el, "use_cases"),
        "Component": lambda el: process_classifier(el, "components"),
        "Enumeration": process_enumeration,
        "Association": process_association,
        "AssociationClass": process_association,
        "Dependency": lambda el: process_dependency(el, "Dependency"),
        "Usage": lambda el: process_dependency(el, "Usage"),
        "Abstraction": lambda el: process_dependency(el, "Abstraction"),
        "Realization": process_realization,
        "InterfaceRealization": process_realization,
    }

    TAG_DISPATCH = {
        "Class": lambda el: process_classifier(el, "classes"),
        "Interface": lambda el: process_classifier(el, "interfaces"),
        "Actor": lambda el: process_classifier(el, "actors"),
        "UseCase": lambda el: process_classifier(el, "use_cases"),
        "Component": lambda el: process_classifier(el, "components"),
        "Enumeration": process_enumeration,
        "Association": process_association,
        "Dependency": lambda el: process_dependency(el, "Dependency"),
        "Usage": lambda el: process_dependency(el, "Usage"),
        "Realization": process_realization,
        "InterfaceRealization": process_realization,
    }

    def walk(el, package_path=""):
        local = _tag_local(el)
        xtype = _xmi_type(el)

        # Model / Package name  (check both tag name and xmi:type)
        is_model   = local == "Model"   or xtype == "Model"
        is_package = local == "Package" or xtype == "Package"
        if (is_model or is_package) and el.get("name"):
            name = el.get("name", "")
            if is_model:
                result["model_name"] = name
            else:
                full_path = f"{package_path}/{name}" if package_path else name
                result["packages"].append(full_path)
                package_path = full_path

        # Dispatch by xmi:type first (most explicit)
        if xtype in TYPE_DISPATCH:
            TYPE_DISPATCH[xtype](el)
        # Then by tag name
        elif local in TAG_DISPATCH:
            TAG_DISPATCH[local](el)

        # Recurse into children for packagedElement, ownedMember, etc.
        for child in el:
            child_local = _tag_local(child)
            if child_local not in (
                "ownedAttribute", "ownedOperation", "ownedEnd",
                "memberEnd", "navigableOwnedEnd", "generalization",
                "ownedComment", "ownedLiteral", "ownedParameter",
                "type", "defaultValue", "lowerValue", "upperValue",
                "TaggedValue",
            ):
                walk(child, package_path)

    walk(root)
    return result


def model_to_text(model: dict) -> str:
    """
    Convert a parsed UML model dict into a readable text summary suitable
    for passing to an LLM.
    """
    lines = [f"UML Model: {model['model_name']}", "=" * 60]

    if model["packages"]:
        lines.append(f"\nPackages ({len(model['packages'])}):")
        for p in model["packages"]:
            lines.append(f"  • {p}")

    def fmt_classifier(c, kind):
        abstract = " (abstract)" if c.get("isAbstract") else ""
        lines.append(f"\n  {kind}: {c['name']}{abstract}")
        if c.get("comments"):
            for comment in c["comments"]:
                lines.append(f"    Description: {comment}")
        if c.get("generalizations"):
            lines.append(f"    Extends: {', '.join(c['generalizations'])}")
        if c.get("attributes"):
            lines.append("    Attributes:")
            for a in c["attributes"]:
                static = " [static]" if a.get("isStatic") else ""
                lines.append(
                    f"      - {a['visibility']} {a['name']}: {a['type'] or 'unspecified'}"
                    f"  [{a.get('multiplicity', '')}]{static}"
                )
        if c.get("operations"):
            lines.append("    Operations:")
            for o in c["operations"]:
                static = " [static]" if o.get("isStatic") else ""
                params = ", ".join(
                    f"{p['name']}: {p['type']}" for p in o.get("parameters", [])
                )
                lines.append(
                    f"      - {o['visibility']} {o['name']}({params})"
                    f": {o.get('returnType', 'void')}{static}"
                )

    if model["classes"]:
        lines.append(f"\nClasses ({len(model['classes'])}):")
        for c in model["classes"]:
            fmt_classifier(c, "Class")

    if model["interfaces"]:
        lines.append(f"\nInterfaces ({len(model['interfaces'])}):")
        for c in model["interfaces"]:
            fmt_classifier(c, "Interface")

    if model["enumerations"]:
        lines.append(f"\nEnumerations ({len(model['enumerations'])}):")
        for e in model["enumerations"]:
            lines.append(f"  • {e['name']}: {', '.join(e['literals'])}")

    if model["actors"]:
        lines.append(f"\nActors ({len(model['actors'])}):")
        for a in model["actors"]:
            lines.append(f"  • {a['name']}")

    if model["use_cases"]:
        lines.append(f"\nUse Cases ({len(model['use_cases'])}):")
        for u in model["use_cases"]:
            lines.append(f"  • {u['name']}")

    if model["components"]:
        lines.append(f"\nComponents ({len(model['components'])}):")
        for c in model["components"]:
            fmt_classifier(c, "Component")

    if model["associations"]:
        lines.append(f"\nAssociations ({len(model['associations'])}):")
        for a in model["associations"]:
            name = f" [{a['name']}]" if a.get("name") else ""
            ends_str = " ↔ ".join(
                f"{e.get('type', '?')} ({e.get('multiplicity', '')})"
                for e in a.get("ends", [])
                if e.get("type")
            )
            lines.append(f"  •{name} {ends_str}")

    if model["dependencies"]:
        lines.append(f"\nDependencies ({len(model['dependencies'])}):")
        for d in model["dependencies"]:
            lines.append(f"  • [{d['kind']}] {d['client']} → {d['supplier']}")

    if model["realizations"]:
        lines.append(f"\nRealizations ({len(model['realizations'])}):")
        for r in model["realizations"]:
            lines.append(f"  • {r['client']} realizes {r['supplier']}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/upload", methods=["POST"])
def upload():
    """Upload and parse an XMI file."""
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "No file selected"}), 400

    filename = file.filename.lower()
    if not (filename.endswith(".xmi") or filename.endswith(".xml")
            or filename.endswith(".uml")):
        return jsonify({"error": "Please upload an XMI, XML, or UML file"}), 400

    try:
        xml_bytes = file.read()
        model = parse_xmi(xml_bytes)
        summary_text = model_to_text(model)
        return jsonify({"model": model, "summary": summary_text})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 422
    except etree.LxmlError as exc:
        return jsonify({"error": f"XML processing error: {exc}"}), 422
    except Exception as exc:  # noqa: BLE001  – catch-all for unexpected I/O errors
        return jsonify({"error": f"Failed to parse XMI: {exc}"}), 500


@app.route("/review", methods=["POST"])
def review():
    """Send the model summary + user question to the LLM for review."""
    data = request.get_json(silent=True) or {}

    api_key = data.get("api_key") or os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        return jsonify({"error": "No OpenAI API key provided"}), 400

    model_summary = data.get("summary", "")
    user_question = data.get("question") or DEFAULT_REVIEW_QUESTION
    llm_model = data.get("model", "gpt-4o-mini")

    if not model_summary:
        return jsonify({"error": "No model summary provided. Upload an XMI file first."}), 400

    system_prompt = (
        "You are an expert UML and software architecture reviewer. "
        "Analyse the provided UML model and answer the user's question. "
        "Be specific, structured, and actionable. "
        "Highlight design patterns, potential issues, missing elements, "
        "and improvement suggestions where relevant."
    )

    user_content = (
        f"Here is the UML model summary:\n\n{model_summary}\n\n"
        f"---\n\nUser question: {user_question}"
    )

    try:
        client = OpenAI(api_key=api_key)
        response = client.chat.completions.create(
            model=llm_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            temperature=0.3,
        )
        answer = response.choices[0].message.content
        return jsonify({"answer": answer})
    except AuthenticationError:
        return jsonify({"error": "Invalid OpenAI API key. Please check your key and try again."}), 401
    except RateLimitError:
        return jsonify({"error": "OpenAI rate limit reached. Please wait a moment and try again."}), 429
    except APIConnectionError as exc:
        return jsonify({"error": f"Could not connect to OpenAI: {exc}"}), 502
    except APIError as exc:
        return jsonify({"error": f"OpenAI API error: {exc}"}), 502
    except Exception as exc:  # noqa: BLE001  – catch-all for unexpected errors
        return jsonify({"error": f"LLM error: {exc}"}), 502


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    debug = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", port=port, debug=debug)
