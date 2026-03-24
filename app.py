"""
XMI UML Review Tool - Flask Backend

A web-based tool for uploading XMI files from UML tools and using
LLM integration to review the underlying UML model.

Supports:
  - XMI 2.x  (standard: Eclipse UML2, MagicDraw/Cameo, Papyrus, Visual Paradigm, Sparx EA)
  - XMI 1.x  (Rational Rose and legacy tools – dot-notation IDs, UML: namespace tags)
  - Enterprise Architect proprietary <xmi:Extension> block (elements, connectors, diagrams)
  - xsi:type attribute (used by some tools instead of xmi:type)
  - Stereotypes, tagged values, profiles
  - State machines and sequence/interaction diagrams
  - HREF-based primitive type resolution
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
# Known UML namespace URIs (XMI 2.x)
# ---------------------------------------------------------------------------
_UML_NS_URIS = {
    "http://www.eclipse.org/uml2/5.0.0/UML",
    "http://www.eclipse.org/uml2/4.0.0/UML",
    "http://www.eclipse.org/uml2/3.0.0/UML",
    "http://www.eclipse.org/uml2/2.1.0/UML",
    "http://www.omg.org/spec/UML/20131001",
    "http://www.omg.org/spec/UML/20110701",
    "http://www.omg.org/spec/UML/20090901",
    "http://schema.omg.org/spec/UML/2.1",
    "http://schema.omg.org/spec/UML/2.0",
}
_XMI_NS_URIS = {
    "http://www.omg.org/XMI",
    "http://www.omg.org/spec/XMI/20131001",
    "http://www.omg.org/spec/XMI/20110701",
}
_XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"

# Map common primitive-type hrefs to readable names
_HREF_TYPE_MAP = {
    "String": "String", "Integer": "Integer", "Boolean": "Boolean",
    "Real": "Real", "UnlimitedNatural": "UnlimitedNatural",
    "int": "int", "long": "long", "double": "double", "float": "float",
    "char": "char", "byte": "byte", "short": "short", "void": "void",
    "bool": "bool", "object": "object",
    # Java
    "java.lang.String": "String", "java.lang.Integer": "Integer",
    "java.lang.Boolean": "Boolean", "java.lang.Long": "Long",
    "java.lang.Double": "Double",
}


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _tag_local(element) -> str:
    """Strip namespace URI from tag and return local name.

    Returns empty string for lxml comment, ProcessingInstruction, and
    Entity nodes, whose ``tag`` attribute is a callable rather than a string.
    """
    tag = element.tag
    if callable(tag):           # lxml Comment, ProcessingInstruction, Entity
        return ""
    if isinstance(tag, bytes):
        tag = tag.decode()
    m = re.match(r"\{[^}]+\}(.+)", tag)
    return m.group(1) if m else tag


def _ns_uri(element) -> str:
    """Return the namespace URI of an element's tag (empty string if none)."""
    tag = element.tag
    if callable(tag) or not tag:
        return ""
    if isinstance(tag, bytes):
        tag = tag.decode()
    m = re.match(r"\{([^}]+)\}", tag)
    return m.group(1) if m else ""


def _get_type_attr(element) -> str:
    """
    Return the element-type classifier string from xmi:type or xsi:type,
    stripping any namespace prefix.  Returns empty string if absent.
    """
    # Try every known XMI namespace
    for ns_uri in _XMI_NS_URIS:
        val = element.get(f"{{{ns_uri}}}type", "")
        if val:
            return val.split(":")[-1]
    # Try xsi:type
    val = element.get(f"{{{_XSI_NS}}}type", "")
    if val:
        return val.split(":")[-1]
    # Plain attributes (older tools omit namespace)
    for attr in ("xmi:type", "type"):
        val = element.get(attr, "")
        if val and ":" in val:
            return val.split(":")[-1]
    return ""


def _get_xmi_id(element) -> str:
    """Return the xmi:id or xmi.id attribute value."""
    for ns_uri in _XMI_NS_URIS:
        val = element.get(f"{{{ns_uri}}}id", "")
        if val:
            return val
    # Dot-notation (XMI 1.x)
    return element.get("xmi.id", element.get("xmi:id", ""))


def _get_xmi_idref(element) -> str:
    """Return xmi:idref / xmi.idref."""
    for ns_uri in _XMI_NS_URIS:
        val = element.get(f"{{{ns_uri}}}idref", "")
        if val:
            return val
    return element.get("xmi.idref", element.get("xmi:idref", ""))


def _resolve_href(href: str) -> str:
    """Convert a type href like 'pathmap://UML_LIBRARIES/UMLPrimitiveTypes.library.uml#String' to 'String'."""
    fragment = href.split("#")[-1].split("/")[-1].split(".")[-1]
    return _HREF_TYPE_MAP.get(fragment, fragment)


def _build_id_map(root) -> dict[str, str]:
    """Two-pass: index every element that has an id/idref and a name."""
    id_name: dict[str, str] = {}

    def _walk(el):
        # Skip comment/PI nodes (their tag is callable, they have no attributes)
        if callable(el.tag):
            return
        xid = _get_xmi_id(el)
        name = el.get("name", "")
        if xid and name:
            id_name[xid] = name
        # Also index dot-notation ids
        dot_id = el.get("xmi.id", "")
        if dot_id and name:
            id_name[dot_id] = name
        for child in el:
            _walk(child)

    _walk(root)
    return id_name


# ---------------------------------------------------------------------------
# Format / tool detection
# ---------------------------------------------------------------------------

def _detect_format(root) -> dict:
    """
    Inspect the root element and its attributes to determine:
      - xmi_version  ("1.x" | "2.x" | "unknown")
      - tool         (e.g. "Enterprise Architect", "MagicDraw", "Papyrus", ...)
      - tool_version (string or "")
    """
    info = {"xmi_version": "unknown", "tool": "Unknown", "tool_version": ""}

    tag_local = _tag_local(root)
    ns_uri = _ns_uri(root)

    # XMI 1.x: root tag is "XMI" (no namespace) with xmi.version attribute
    xmi_dot_ver = root.get("xmi.version", "")
    if xmi_dot_ver:
        info["xmi_version"] = f"1.x ({xmi_dot_ver})"
        # Try to detect tool from exporter — can be child element text OR attribute
        for child in root:
            if _tag_local(child) in ("XMI.header", "header"):
                for sub in child:
                    if _tag_local(sub) in ("XMI.documentation", "documentation"):
                        # Try attributes first
                        exporter = sub.get("xmi.exporter", sub.get("exporter", ""))
                        ver = sub.get("xmi.exporterVersion", sub.get("exporterVersion", ""))
                        # Fall back to child element text (Rational Rose style)
                        for gg in sub:
                            gg_local = _tag_local(gg)
                            if gg_local in ("XMI.exporter", "exporter") and not exporter:
                                exporter = (gg.text or "").strip()
                            elif gg_local in ("XMI.exporterVersion", "exporterVersion") and not ver:
                                ver = (gg.text or "").strip()
                        if exporter:
                            info["tool"] = exporter
                            info["tool_version"] = ver
        if info["tool"] == "Unknown":
            info["tool"] = "Legacy UML tool (XMI 1.x)"
        return info

    # XMI 2.x: look for xmi:XMI root or uml:Model root
    xmi_colon_ver = ""
    for ns_uri_key in _XMI_NS_URIS:
        xmi_colon_ver = root.get(f"{{{ns_uri_key}}}version", "")
        if xmi_colon_ver:
            break
    if not xmi_colon_ver:
        xmi_colon_ver = root.get("xmi:version", "")
    if xmi_colon_ver:
        info["xmi_version"] = f"2.x ({xmi_colon_ver})"

    # Detect specific tools from Documentation / Exporter tags
    for child in root:
        local = _tag_local(child)
        if local in ("Documentation", "XMI.documentation"):
            exporter = child.get("exporter", child.get("xmi.exporter", ""))
            ver = child.get("exporterVersion", child.get("exporterVersion", ""))
            if exporter:
                info["tool"] = exporter
                info["tool_version"] = ver
            break
        # MagicDraw / Cameo stores tool in a different attribute
        if local == "umlVersion" or "magicdraw" in child.get("exporter", "").lower():
            info["tool"] = "MagicDraw / Cameo"
            break

    # EA-specific: check for Extension extender attribute
    for child in root:
        local = _tag_local(child)
        extender = child.get("extender", "")
        if local == "Extension" and extender:
            info["tool"] = extender
            break

    # Fallback: guess from namespace URIs present in the document
    if info["tool"] == "Unknown":
        # Check each namespace URI directly instead of substring-matching
        # the full nsmap string representation.
        ns_values: list[str] = [v for v in (root.nsmap or {}).values() if v]
        tag_str = root.tag if isinstance(root.tag, str) else ""

        def _ns_contains(fragment: str) -> bool:
            """True if fragment appears in the tag or any declared namespace URI."""
            if fragment in tag_str:
                return True
            return any(fragment in ns for ns in ns_values)

        if _ns_contains("eclipse.org"):
            info["tool"] = "Eclipse-based tool (Papyrus / UML2)"
        elif _ns_contains("omg.org"):
            info["tool"] = "OMG-compliant tool"

    if info["xmi_version"] == "unknown" and (
        tag_local in ("XMI", "Model") or any(u in _UML_NS_URIS for u in root.nsmap.values())
    ):
        info["xmi_version"] = "2.x"

    return info


# ---------------------------------------------------------------------------
# XMI 1.x parser  (Rational Rose and other legacy tools)
# ---------------------------------------------------------------------------

def _parse_v1(root) -> dict:
    """Parse XMI 1.x files (dot-notation IDs, UML: namespace tags)."""

    result = _empty_result()
    id_name = _build_id_map(root)

    def resolve(ref: str) -> str:
        return id_name.get(ref, ref) if ref else ref

    def _v1_multiplicity(end_el):
        """Extract multiplicity range from a 1.x AssociationEnd."""
        lo, hi = "0", "*"
        for mult in end_el.iter():
            if _tag_local(mult) == "MultiplicityRange":
                lo = mult.get("lower", "0")
                hi_raw = mult.get("upper", "-1")
                hi = "*" if hi_raw == "-1" else hi_raw
        return f"{lo}..{hi}"

    def _v1_attr_type(attr_el):
        """Resolve attribute type for XMI 1.x element."""
        type_ref = attr_el.get("type", "")
        if type_ref:
            return resolve(type_ref)
        for child in attr_el.iter():
            local = _tag_local(child)
            if local in ("DataType", "Class", "Interface"):
                href = child.get("href", "")
                if href:
                    return _resolve_href(href)
                name = child.get("name", child.get("xmi.idref", ""))
                if name:
                    return resolve(name)
        return ""

    def walk_v1(el, package_path=""):
        if callable(el.tag):   # skip comment/PI nodes
            return
        local = _tag_local(el)
        name = el.get("name", "")

        # Model
        if local in ("Model", "UML:Model") and name:
            result["model_name"] = name

        # Package
        elif local in ("Package", "UML:Package", "Model_Management.Package",
                       "Subsystem") and name:
            full = f"{package_path}/{name}" if package_path else name
            result["packages"].append(full)
            for child in el:
                walk_v1(child, full)
            return

        # Class
        elif local in ("Class", "UML:Class", "Foundation.Core.Class"):
            entry = {
                "name": name,
                "visibility": el.get("visibility", "public"),
                "isAbstract": el.get("isAbstract", "false") == "true",
                "attributes": [],
                "operations": [],
                "generalizations": [],
                "comments": [],
                "stereotypes": [],
            }

            def _add_v1_features(feature_el):
                """Process a single attribute/operation/generalization element."""
                clocal = _tag_local(feature_el)
                # Attributes
                if clocal in ("Attribute", "UML:Attribute",
                              "Foundation.Core.Attribute"):
                    visi = feature_el.get("visibility", "private")
                    type_name = _v1_attr_type(feature_el)
                    entry["attributes"].append({
                        "name": feature_el.get("name", ""),
                        "type": type_name,
                        "visibility": visi,
                        "isStatic": feature_el.get("ownerScope", "") == "classifier",
                        "multiplicity": "1..1",
                    })
                # Operations
                elif clocal in ("Operation", "UML:Operation",
                                "Foundation.Core.Operation"):
                    params, ret_type = [], "void"
                    for pc in feature_el.iter():
                        if _tag_local(pc) in ("Parameter", "UML:Parameter"):
                            kind = pc.get("kind", pc.get("direction", "in"))
                            if kind == "return":
                                ret_type = _v1_attr_type(pc) or "void"
                            else:
                                params.append({
                                    "name": pc.get("name", ""),
                                    "type": _v1_attr_type(pc),
                                    "direction": kind,
                                })
                    entry["operations"].append({
                        "name": feature_el.get("name", ""),
                        "visibility": feature_el.get("visibility", "public"),
                        "isStatic": feature_el.get("ownerScope", "") == "classifier",
                        "parameters": params,
                        "returnType": ret_type,
                    })
                # Generalization
                elif clocal in ("Generalization", "UML:Generalization",
                                "Foundation.Core.Generalization"):
                    parent_ref = feature_el.get("parent", feature_el.get("supertype", ""))
                    if parent_ref:
                        entry["generalizations"].append(resolve(parent_ref))
                elif clocal in ("Comment", "UML:Comment"):
                    body = feature_el.get("body", feature_el.get("name", ""))
                    if body:
                        entry["comments"].append(body)

            for child in el:
                clocal = _tag_local(child)
                # Container elements (Rational Rose & Eclipse UML2 style)
                if clocal in ("Classifier.feature", "UML:Classifier.feature",
                              "Foundation.Core.Classifier.feature",
                              "GeneralizableElement.generalization",
                              "UML:GeneralizableElement.generalization"):
                    for gc in child:
                        _add_v1_features(gc)
                else:
                    _add_v1_features(child)

            result["classes"].append(entry)

        # Interface
        elif local in ("Interface", "UML:Interface",
                       "Foundation.Core.Interface") and name:
            entry = {
                "name": name,
                "visibility": el.get("visibility", "public"),
                "isAbstract": True,
                "attributes": [],
                "operations": [],
                "generalizations": [],
                "comments": [],
                "stereotypes": [],
            }
            result["interfaces"].append(entry)

        # Enumeration / DataType used as enum
        elif local in ("Enumeration", "DataType") and name:
            literals = [
                child.get("name", "") for child in el
                if _tag_local(child) in ("EnumerationLiteral", "Literal")
            ]
            result["enumerations"].append({"name": name, "literals": literals})

        # Association
        elif local in ("Association", "UML:Association",
                       "Foundation.Core.Association") and not name.startswith("#"):
            ends = []
            for child in el:
                if _tag_local(child) in (
                    "AssociationEnd", "UML:AssociationEnd",
                    "Foundation.Core.AssociationEnd",
                    "Connection",   # some tools wrap AssociationEnds in Connection
                ):
                    # Direct AssociationEnd
                    if _tag_local(child) == "Connection":
                        for ae in child:
                            type_ref = ae.get("type", "")
                            ends.append({
                                "name": ae.get("name", ""),
                                "type": resolve(type_ref),
                                "aggregation": ae.get("aggregation", "none"),
                                "multiplicity": _v1_multiplicity(ae),
                                "navigable": ae.get("isNavigable", "true") == "true",
                            })
                    else:
                        type_ref = child.get("type", "")
                        ends.append({
                            "name": child.get("name", ""),
                            "type": resolve(type_ref),
                            "aggregation": child.get("aggregation", "none"),
                            "multiplicity": _v1_multiplicity(child),
                            "navigable": child.get("isNavigable", "true") == "true",
                        })
            result["associations"].append({"name": name, "ends": ends})

        # Dependency / Usage
        elif local in ("Dependency", "UML:Dependency",
                       "Foundation.Core.Dependency"):
            client_ref = el.get("client", "")
            supplier_ref = el.get("supplier", "")
            result["dependencies"].append({
                "kind": "Dependency",
                "name": name,
                "client": resolve(client_ref),
                "supplier": resolve(supplier_ref),
            })

        # Actor / UseCase
        elif local in ("Actor", "UML:Actor") and name:
            result["actors"].append({
                "name": name, "visibility": "public",
                "isAbstract": False, "attributes": [],
                "operations": [], "generalizations": [], "comments": [],
                "stereotypes": [],
            })
        elif local in ("UseCase", "UML:UseCase") and name:
            result["use_cases"].append({
                "name": name, "visibility": "public",
                "isAbstract": False, "attributes": [],
                "operations": [], "generalizations": [], "comments": [],
                "stereotypes": [],
            })

        for child in el:
            walk_v1(child, package_path)

    walk_v1(root)
    return result


# ---------------------------------------------------------------------------
# XMI 2.x parser (standard + EA extension)
# ---------------------------------------------------------------------------

def _empty_result() -> dict:
    return {
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
        "state_machines": [],
        "interactions": [],
        "diagrams": [],
        "stereotypes_used": [],
        "warnings": [],
    }


def _resolve_href_type(attr_el, id_name: dict) -> str:
    """Try every mechanism to find the type name for an attribute/parameter."""
    type_ref = attr_el.get("type", "")
    if type_ref:
        return id_name.get(type_ref, type_ref)
    # Inline <type> child
    for child in attr_el:
        if _tag_local(child) == "type":
            href = (child.get(f"{{{_XSI_NS}}}href", "")
                    or child.get("href", "")
                    or child.get("{http://www.w3.org/1999/xlink}href", ""))
            if href:
                return _resolve_href(href)
            idref = _get_xmi_idref(child)
            if idref:
                return id_name.get(idref, idref)
            name = child.get("name", "")
            if name:
                return name
    return ""


def _parse_v2_standard(root, id_name: dict) -> dict:
    """Parse the main XMI 2.x structural content."""
    result = _empty_result()

    def resolve(ref: str) -> str:
        return id_name.get(ref, ref) if ref else ref

    def _parse_attr(el):
        name = el.get("name", "")
        type_name = _resolve_href_type(el, id_name)
        lo = el.get("lower", "1")
        hi = el.get("upper", "1")
        # lowerValue / upperValue child elements
        for child in el:
            if _tag_local(child) == "lowerValue":
                lo = child.get("value", lo)
            elif _tag_local(child) == "upperValue":
                hi = child.get("value", hi)
        return {
            "name": name,
            "type": type_name,
            "visibility": el.get("visibility", "public"),
            "isStatic": el.get("isStatic", "false") == "true",
            "multiplicity": f"{lo}..{hi}",
        }

    def _parse_operation(el):
        name = el.get("name", "")
        params, ret_type = [], "void"
        for child in el:
            if _tag_local(child) == "ownedParameter":
                direction = child.get("direction", "in")
                type_name = _resolve_href_type(child, id_name)
                if direction == "return":
                    ret_type = type_name or "void"
                else:
                    params.append({
                        "name": child.get("name", ""),
                        "type": type_name,
                        "direction": direction,
                    })
        return {
            "name": name,
            "visibility": el.get("visibility", "public"),
            "isStatic": el.get("isStatic", "false") == "true",
            "parameters": params,
            "returnType": ret_type,
        }

    def _parse_stereotypes(el) -> list[str]:
        """Extract stereotype names applied to this element."""
        stereos = []
        for child in el:
            if _tag_local(child) == "appliedStereotype":
                ref = _get_xmi_idref(child)
                if ref:
                    name = resolve(ref)
                    if name and name != ref:
                        stereos.append(name)
        return stereos

    def _parse_tagged_values(el) -> list[dict]:
        """Extract tagged values."""
        tvs = []
        for child in el:
            local = _tag_local(child)
            if local in ("TaggedValue", "tag"):
                tag_name = child.get("tag", child.get("name", ""))
                tag_val = child.get("value", child.get("body", ""))
                if tag_name:
                    tvs.append({"tag": tag_name, "value": tag_val})
        return tvs

    def _process_classifier(el, category):
        name = el.get("name", "")
        if not name:
            return
        entry = {
            "name": name,
            "visibility": el.get("visibility", "public"),
            "isAbstract": el.get("isAbstract", "false") == "true",
            "attributes": [],
            "operations": [],
            "generalizations": [],
            "comments": [],
            "stereotypes": _parse_stereotypes(el),
            "tagged_values": _parse_tagged_values(el),
        }
        for child in el:
            local = _tag_local(child)
            if local == "ownedAttribute":
                entry["attributes"].append(_parse_attr(child))
            elif local == "ownedOperation":
                entry["operations"].append(_parse_operation(child))
            elif local == "generalization":
                general_ref = child.get("general", "")
                if general_ref:
                    entry["generalizations"].append(resolve(general_ref))
            elif local == "ownedComment":
                body = child.get("body", "")
                if body:
                    entry["comments"].append(body)
        result[category].append(entry)

    def _process_enumeration(el):
        name = el.get("name", "")
        if not name:
            return
        literals = []
        for child in el:
            if _tag_local(child) == "ownedLiteral":
                literals.append(child.get("name", ""))
        result["enumerations"].append({"name": name, "literals": literals})

    def _process_association(el):
        ends = []
        for child in el:
            local = _tag_local(child)
            if local in ("ownedEnd", "navigableOwnedEnd"):
                type_ref = child.get("type", "")
                agg = child.get("aggregation", "none")
                lo, hi = child.get("lower", "0"), child.get("upper", "*")
                for gc in child:
                    if _tag_local(gc) == "lowerValue":
                        lo = gc.get("value", lo)
                    elif _tag_local(gc) == "upperValue":
                        hi = gc.get("value", hi)
                ends.append({
                    "name": child.get("name", ""),
                    "type": resolve(type_ref),
                    "aggregation": agg,
                    "multiplicity": f"{lo}..{hi}",
                    "navigable": local == "navigableOwnedEnd",
                })
        # memberEnd IDREF list fallback
        if not ends:
            for ref in el.get("memberEnd", "").split():
                ends.append({"type": resolve(ref), "name": "",
                             "aggregation": "none", "multiplicity": ""})
        result["associations"].append({"name": el.get("name", ""), "ends": ends})

    def _process_dependency(el, kind):
        client = resolve(el.get("client", ""))
        supplier = resolve(el.get("supplier", ""))
        result["dependencies"].append({
            "kind": kind,
            "name": el.get("name", ""),
            "client": client,
            "supplier": supplier,
        })

    def _process_realization(el):
        result["realizations"].append({
            "name": el.get("name", ""),
            "client": resolve(el.get("client", "")),
            "supplier": resolve(el.get("supplier", "")),
        })

    def _process_state_machine(el):
        sm_name = el.get("name", "")
        states, transitions = [], []
        for child in el.iter():
            local = _tag_local(child)
            if local in ("subvertex", "State", "Pseudostate", "FinalState"):
                s_name = child.get("name", "")
                s_kind = _get_type_attr(child) or local
                if s_name:
                    states.append({"name": s_name, "kind": s_kind})
            elif local in ("transition", "Transition"):
                src = resolve(child.get("source", ""))
                tgt = resolve(child.get("target", ""))
                guard_text = ""
                for gc in child:
                    if _tag_local(gc) == "guard":
                        for ggc in gc:
                            if _tag_local(ggc) == "specification":
                                guard_text = ggc.get("value", ggc.get("body", ""))
                transitions.append({
                    "source": src, "target": tgt,
                    "trigger": child.get("name", ""),
                    "guard": guard_text,
                })
        result["state_machines"].append({
            "name": sm_name,
            "states": states,
            "transitions": transitions,
        })

    def _process_interaction(el):
        name = el.get("name", "")
        lifelines = []
        messages = []
        for child in el:
            local = _tag_local(child)
            if local == "lifeline":
                ll_name = child.get("name", "")
                repr_ref = child.get("represents", "")
                lifelines.append({
                    "name": ll_name,
                    "represents": resolve(repr_ref),
                })
            elif local == "message":
                msg_name = child.get("name", "")
                send_ref = child.get("sendEvent", "")
                recv_ref = child.get("receiveEvent", "")
                messages.append({
                    "name": msg_name,
                    "sort": child.get("messageSort", "synchCall"),
                    "from": resolve(send_ref),
                    "to": resolve(recv_ref),
                    "signature": resolve(child.get("signature", "")),
                })
        result["interactions"].append({
            "name": name,
            "lifelines": lifelines,
            "messages": messages,
        })

    TYPE_DISPATCH = {
        "Class": lambda el: _process_classifier(el, "classes"),
        "Interface": lambda el: _process_classifier(el, "interfaces"),
        "Actor": lambda el: _process_classifier(el, "actors"),
        "UseCase": lambda el: _process_classifier(el, "use_cases"),
        "Component": lambda el: _process_classifier(el, "components"),
        "Subsystem": lambda el: _process_classifier(el, "components"),
        "Enumeration": _process_enumeration,
        # Some tools (e.g. Rational Rose) export simple value types as
        # uml:DataType elements with ownedLiteral children — treat them
        # as enumerations so their literals aren't silently discarded.
        "DataType": _process_enumeration,
        "PrimitiveType": lambda el: None,  # skip – just type stubs
        "Association": _process_association,
        "AssociationClass": _process_association,
        "Dependency": lambda el: _process_dependency(el, "Dependency"),
        "Usage": lambda el: _process_dependency(el, "Usage"),
        "Abstraction": lambda el: _process_dependency(el, "Abstraction"),
        "Realization": _process_realization,
        "InterfaceRealization": _process_realization,
        "StateMachine": _process_state_machine,
        "Interaction": _process_interaction,
    }

    TAG_DISPATCH = {
        "Class": lambda el: _process_classifier(el, "classes"),
        "Interface": lambda el: _process_classifier(el, "interfaces"),
        "Actor": lambda el: _process_classifier(el, "actors"),
        "UseCase": lambda el: _process_classifier(el, "use_cases"),
        "Component": lambda el: _process_classifier(el, "components"),
        "Enumeration": _process_enumeration,
        "Association": _process_association,
        "Dependency": lambda el: _process_dependency(el, "Dependency"),
        "Usage": lambda el: _process_dependency(el, "Usage"),
        "Realization": _process_realization,
        "InterfaceRealization": _process_realization,
        "StateMachine": _process_state_machine,
        "Interaction": _process_interaction,
    }

    # Children whose subtrees are fully consumed by parent parsers
    _LEAF_LOCALS = frozenset({
        "ownedAttribute", "ownedOperation", "ownedEnd",
        "navigableOwnedEnd", "generalization", "ownedComment",
        "ownedLiteral", "ownedParameter", "type", "defaultValue",
        "lowerValue", "upperValue", "TaggedValue", "appliedStereotype",
        "tag", "guard", "trigger", "effect",
    })

    def walk(el, package_path=""):
        if callable(el.tag):   # skip comment/PI nodes
            return
        local = _tag_local(el)
        xtype = _get_type_attr(el)

        # Skip the EA Extension block here – handled separately
        if local == "Extension" and el.get("extender", "").lower().startswith("enterprise"):
            return

        # Model / Package
        is_model = local == "Model" or xtype == "Model"
        is_pkg = local == "Package" or xtype == "Package"
        if (is_model or is_pkg) and el.get("name"):
            n = el.get("name", "")
            if is_model:
                result["model_name"] = n
            else:
                full = f"{package_path}/{n}" if package_path else n
                result["packages"].append(full)
                package_path = full

        # Dispatch
        if xtype in TYPE_DISPATCH:
            TYPE_DISPATCH[xtype](el)
        elif local in TAG_DISPATCH:
            TAG_DISPATCH[local](el)

        for child in el:
            if _tag_local(child) not in _LEAF_LOCALS:
                walk(child, package_path)

    walk(root)
    return result


def _parse_ea_extension(root, result: dict, id_name: dict) -> None:
    """
    Parse Enterprise Architect's proprietary <xmi:Extension> block.

    This block carries details that EA does NOT put in the standard XMI
    body: full attribute/operation lists, connector (association) details,
    documentation notes, and diagram names.  We merge the richer EA data
    on top of the standard-parse result.
    """
    extension = None
    for child in root:
        local = _tag_local(child)
        extender = child.get("extender", "")
        if local == "Extension" and extender.lower().startswith("enterprise"):
            extension = child
            break
    if extension is None:
        return

    def resolve(ref: str) -> str:
        return id_name.get(ref, ref) if ref else ref

    # ── Index existing classified items by name for merging ──────────────
    existing: dict[str, dict] = {}
    for category in ("classes", "interfaces", "components",
                      "actors", "use_cases"):
        for item in result[category]:
            existing[item["name"]] = item

    # ── <elements> block ─────────────────────────────────────────────────
    for elements_el in extension:
        if _tag_local(elements_el) != "elements":
            continue
        for el in elements_el:
            if _tag_local(el) != "element":
                continue
            name = el.get("name", "")
            ea_type = el.get(
                "{http://www.omg.org/XMI}type",
                el.get("xmi:type", el.get("type", ""))
            ).split(":")[-1]

            # Documentation / notes
            doc = ""
            for child in el:
                if _tag_local(child) == "properties":
                    doc = child.get("documentation", child.get("alias", ""))

            # Attributes from EA's detail block
            ea_attrs = []
            for attrs_el in el:
                if _tag_local(attrs_el) != "attributes":
                    continue
                for attr in attrs_el:
                    if _tag_local(attr) != "attribute":
                        continue
                    a_name = attr.get("name", "")
                    a_type, a_scope, a_static = "", "private", False
                    for ac in attr:
                        ac_local = _tag_local(ac)
                        if ac_local == "model":
                            a_type = ac.get("type", "")
                            a_scope = ac.get("scope", "private")
                            a_static = ac.get("static", "false") == "true"
                    ea_attrs.append({
                        "name": a_name,
                        "type": a_type,
                        "visibility": a_scope,
                        "isStatic": a_static,
                        "multiplicity": "1..1",
                    })

            # Operations from EA's detail block
            ea_ops = []
            for ops_el in el:
                if _tag_local(ops_el) != "operations":
                    continue
                for op in ops_el:
                    if _tag_local(op) != "operation":
                        continue
                    o_name = op.get("name", "")
                    o_type, o_scope, o_static = "void", "public", False
                    params = []
                    for oc in op:
                        oc_local = _tag_local(oc)
                        if oc_local == "model":
                            o_type = oc.get("type", "void") or "void"
                            o_scope = oc.get("scope", "public")
                            o_static = oc.get("static", "false") == "true"
                        elif oc_local == "parameters":
                            for param in oc:
                                if _tag_local(param) == "parameter":
                                    p_name = param.get("name", "")
                                    p_type = ""
                                    for pc in param:
                                        if _tag_local(pc) == "model":
                                            p_type = pc.get("type", "")
                                    if p_name and p_name != "return":
                                        params.append({
                                            "name": p_name,
                                            "type": p_type,
                                            "direction": "in",
                                        })
                    ea_ops.append({
                        "name": o_name,
                        "visibility": o_scope,
                        "isStatic": o_static,
                        "parameters": params,
                        "returnType": o_type,
                    })

            # Stereotypes
            stereos = []
            for child in el:
                if _tag_local(child) == "stereotype":
                    stereo_name = child.get("stereotype", child.get("name", ""))
                    if stereo_name:
                        stereos.append(stereo_name)

            if name in existing:
                # Merge: prefer EA detail if the standard parse gave nothing
                item = existing[name]
                if ea_attrs and not item.get("attributes"):
                    item["attributes"] = ea_attrs
                elif ea_attrs:
                    # EA has more detail on types — update where blank
                    for ea_a in ea_attrs:
                        for std_a in item["attributes"]:
                            if std_a["name"] == ea_a["name"] and not std_a.get("type"):
                                std_a["type"] = ea_a["type"]
                if ea_ops and not item.get("operations"):
                    item["operations"] = ea_ops
                if doc and not item.get("comments"):
                    item["comments"] = [doc]
                if stereos:
                    item.setdefault("stereotypes", [])
                    for s in stereos:
                        if s not in item["stereotypes"]:
                            item["stereotypes"].append(s)
            else:
                # EA element not found in standard parse — add it
                if ea_type in ("Class", ""):
                    cat = "classes"
                elif ea_type == "Interface":
                    cat = "interfaces"
                elif ea_type == "Component":
                    cat = "components"
                elif ea_type == "Actor":
                    cat = "actors"
                elif ea_type == "UseCase":
                    cat = "use_cases"
                elif ea_type == "Enumeration":
                    result["enumerations"].append({"name": name, "literals": []})
                    continue
                else:
                    continue
                new_entry = {
                    "name": name,
                    "visibility": "public",
                    "isAbstract": False,
                    "attributes": ea_attrs,
                    "operations": ea_ops,
                    "generalizations": [],
                    "comments": [doc] if doc else [],
                    "stereotypes": stereos,
                    "tagged_values": [],
                }
                result[cat].append(new_entry)
                existing[name] = new_entry

    # ── <connectors> block ────────────────────────────────────────────────
    # Build a name→class dict once so generalisation lookup is O(1) not O(n).
    class_by_name: dict[str, dict] = {c["name"]: c for c in result["classes"]}
    for connectors_el in extension:
        if _tag_local(connectors_el) != "connectors":
            continue
        for conn in connectors_el:
            if _tag_local(conn) != "connector":
                continue
            props = conn.find("properties") if hasattr(conn, "find") else None
            ea_type_c = ""
            direction = ""
            if props is not None:
                ea_type_c = props.get("ea_type", "")
                direction = props.get("direction", "")

            src_name, tgt_name = "", ""
            src_role, tgt_role = "", ""
            src_agg, tgt_agg = "none", "none"
            src_mult, tgt_mult = "", ""

            for child in conn:
                clocal = _tag_local(child)
                if clocal == "source":
                    for sc in child:
                        sc_local = _tag_local(sc)
                        if sc_local == "model":
                            src_name = sc.get("name", "")
                        elif sc_local == "role":
                            src_role = sc.get("name", "")
                        elif sc_local == "type":
                            agg_val = sc.get("aggregation", "0")
                            src_agg = (
                                "composite" if agg_val == "2"
                                else "shared" if agg_val == "1"
                                else "none"
                            )
                        elif sc_local == "multiplicity":
                            lo = sc.get("lower", "")
                            hi = sc.get("upper", "")
                            if lo or hi:
                                src_mult = f"{lo}..{hi}"
                elif clocal == "target":
                    for tc in child:
                        tc_local = _tag_local(tc)
                        if tc_local == "model":
                            tgt_name = tc.get("name", "")
                        elif tc_local == "role":
                            tgt_role = tc.get("name", "")
                        elif tc_local == "type":
                            agg_val = tc.get("aggregation", "0")
                            tgt_agg = (
                                "composite" if agg_val == "2"
                                else "shared" if agg_val == "1"
                                else "none"
                            )
                        elif tc_local == "multiplicity":
                            lo = tc.get("lower", "")
                            hi = tc.get("upper", "")
                            if lo or hi:
                                tgt_mult = f"{lo}..{hi}"

            if ea_type_c in ("Association", "Aggregation", "Composition", ""):
                # Map to association
                ends = [
                    {"name": src_role, "type": src_name,
                     "aggregation": src_agg, "multiplicity": src_mult,
                     "navigable": False},
                    {"name": tgt_role, "type": tgt_name,
                     "aggregation": tgt_agg, "multiplicity": tgt_mult,
                     "navigable": True},
                ]
                result["associations"].append({
                    "name": conn.get("{http://www.omg.org/XMI}id",
                                    conn.get("xmi:id", "")),
                    "ends": ends,
                })
            elif ea_type_c == "Dependency":
                result["dependencies"].append({
                    "kind": "Dependency",
                    "name": "",
                    "client": src_name,
                    "supplier": tgt_name,
                })
            elif ea_type_c in ("Realization", "Realisation"):
                result["realizations"].append({
                    "name": "",
                    "client": src_name,
                    "supplier": tgt_name,
                })
            elif ea_type_c == "Generalization":
                # Add to child class's generalization list (O(1) dict lookup)
                child_cls = class_by_name.get(src_name)
                if child_cls is not None:
                    if tgt_name not in child_cls["generalizations"]:
                        child_cls["generalizations"].append(tgt_name)

    # ── <diagrams> block ──────────────────────────────────────────────────
    for diagrams_el in extension:
        if _tag_local(diagrams_el) != "diagrams":
            continue
        for diag in diagrams_el:
            if _tag_local(diag) != "diagram":
                continue
            d_name, d_type = "", ""
            for child in diag:
                if _tag_local(child) == "properties":
                    d_name = child.get("name", "")
                    d_type = child.get("type", child.get("diagramType", ""))
            if d_name:
                result["diagrams"].append({"name": d_name, "type": d_type})


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------

def parse_xmi(xml_bytes: bytes) -> dict:
    """
    Parse an XMI byte string and return a structured dict describing the
    UML model.

    Strategy:
      1. Parse XML with lxml.
      2. Detect format (XMI 1.x vs 2.x) and exporting tool.
      3. Route to the appropriate parser strategy.
      4. If Enterprise Architect, overlay the proprietary Extension block data.
      5. Collect unique stereotypes used across the model.
      6. Return a rich result dict with diagnostics.
    """
    try:
        root = etree.fromstring(xml_bytes)
    except etree.XMLSyntaxError as exc:
        raise ValueError(f"Invalid XML/XMI file: {exc}") from exc

    fmt = _detect_format(root)

    # Build global id→name map (works for both 1.x and 2.x)
    id_name = _build_id_map(root)

    # Choose parse strategy
    xmi_ver = fmt["xmi_version"]
    if xmi_ver.startswith("1."):
        result = _parse_v1(root)
    else:
        result = _parse_v2_standard(root, id_name)
        # Overlay EA extension data (no-op if not EA)
        _parse_ea_extension(root, result, id_name)

    # Attach metadata
    result["format_info"] = fmt

    # De-duplicate associations added from both standard + EA extension
    seen_assoc: set[str] = set()
    deduped = []
    for a in result["associations"]:
        ends_key = "|".join(sorted(e.get("type", "") for e in a["ends"]))
        if ends_key and ends_key not in seen_assoc:
            seen_assoc.add(ends_key)
            deduped.append(a)
    result["associations"] = deduped

    # Collect all stereotypes used across the model for the UI summary
    all_stereos: set[str] = set()
    for category in ("classes", "interfaces", "components", "actors",
                      "use_cases"):
        for item in result.get(category, []):
            for s in item.get("stereotypes", []):
                if s:
                    all_stereos.add(s)
    result["stereotypes_used"] = sorted(all_stereos)

    return result


# ---------------------------------------------------------------------------
# Text summary for the LLM
# ---------------------------------------------------------------------------

def model_to_text(model: dict) -> str:
    """
    Convert a parsed UML model dict into a detailed, readable text summary
    suitable for passing to an LLM.
    """
    lines = []
    fmt = model.get("format_info", {})
    tool = fmt.get("tool", "")
    xmi_ver = fmt.get("xmi_version", "")
    tool_ver = fmt.get("tool_version", "")

    # Header
    lines.append(f"UML Model: {model['model_name']}")
    lines.append("=" * 60)
    if tool:
        tv = f" v{tool_ver}" if tool_ver else ""
        lines.append(f"Exported by: {tool}{tv}  |  XMI version: {xmi_ver}")

    if model.get("stereotypes_used"):
        lines.append(f"Stereotypes in use: {', '.join(model['stereotypes_used'])}")

    if model.get("diagrams"):
        lines.append(
            f"Diagrams ({len(model['diagrams'])}): "
            + ", ".join(
                f"{d['name']} [{d['type']}]" if d.get("type") else d["name"]
                for d in model["diagrams"]
            )
        )

    if model.get("packages"):
        lines.append(f"\nPackages ({len(model['packages'])}):")
        for p in model["packages"]:
            lines.append(f"  • {p}")

    def fmt_classifier(c, kind):
        abstract = " (abstract)" if c.get("isAbstract") else ""
        stereo_str = ""
        if c.get("stereotypes"):
            stereo_str = f"  «{', '.join(c['stereotypes'])}»"
        lines.append(f"\n  {kind}: {c['name']}{abstract}{stereo_str}")
        for comment in c.get("comments", []):
            lines.append(f"    Description: {comment}")
        if c.get("generalizations"):
            lines.append(f"    Extends: {', '.join(c['generalizations'])}")
        if c.get("tagged_values"):
            tv_pairs = ", ".join(
                f"{tv['tag']}={tv['value']}" for tv in c["tagged_values"]
            )
            lines.append(f"    Tagged values: {tv_pairs}")
        if c.get("attributes"):
            lines.append("    Attributes:")
            for a in c["attributes"]:
                static = " [static]" if a.get("isStatic") else ""
                lines.append(
                    f"      - {a['visibility']} {a['name']}: "
                    f"{a['type'] or 'unspecified'}  "
                    f"[{a.get('multiplicity', '')}]{static}"
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

    if model.get("classes"):
        lines.append(f"\nClasses ({len(model['classes'])}):")
        for c in model["classes"]:
            fmt_classifier(c, "Class")

    if model.get("interfaces"):
        lines.append(f"\nInterfaces ({len(model['interfaces'])}):")
        for c in model["interfaces"]:
            fmt_classifier(c, "Interface")

    if model.get("enumerations"):
        lines.append(f"\nEnumerations ({len(model['enumerations'])}):")
        for e in model["enumerations"]:
            lines.append(f"  • {e['name']}: {', '.join(e['literals'])}")

    if model.get("actors"):
        lines.append(f"\nActors ({len(model['actors'])}):")
        for a in model["actors"]:
            stereo_str = (f"  «{', '.join(a.get('stereotypes', []))}»"
                          if a.get("stereotypes") else "")
            lines.append(f"  • {a['name']}{stereo_str}")

    if model.get("use_cases"):
        lines.append(f"\nUse Cases ({len(model['use_cases'])}):")
        for u in model["use_cases"]:
            lines.append(f"  • {u['name']}")

    if model.get("components"):
        lines.append(f"\nComponents ({len(model['components'])}):")
        for c in model["components"]:
            fmt_classifier(c, "Component")

    if model.get("associations"):
        lines.append(f"\nAssociations ({len(model['associations'])}):")
        for a in model["associations"]:
            name_str = f" [{a['name']}]" if a.get("name") else ""
            ends_parts = []
            for e in a.get("ends", []):
                if not e.get("type"):
                    continue
                role = f" (role: {e['name']})" if e.get("name") else ""
                agg = e.get("aggregation", "none")
                agg_str = f" [{agg}]" if agg and agg != "none" else ""
                mult = f" {e.get('multiplicity', '')}" if e.get("multiplicity") else ""
                ends_parts.append(f"{e['type']}{role}{mult}{agg_str}")
            lines.append(f"  •{name_str} " + " ↔ ".join(ends_parts))

    if model.get("dependencies"):
        lines.append(f"\nDependencies ({len(model['dependencies'])}):")
        for d in model["dependencies"]:
            lines.append(f"  • [{d['kind']}] {d['client']} → {d['supplier']}")

    if model.get("realizations"):
        lines.append(f"\nRealizations ({len(model['realizations'])}):")
        for r in model["realizations"]:
            lines.append(f"  • {r['client']} realizes {r['supplier']}")

    if model.get("state_machines"):
        lines.append(f"\nState Machines ({len(model['state_machines'])}):")
        for sm in model["state_machines"]:
            lines.append(f"  • {sm['name']}")
            if sm.get("states"):
                lines.append(
                    f"    States: {', '.join(s['name'] for s in sm['states'])}"
                )
            if sm.get("transitions"):
                for t in sm["transitions"]:
                    guard = f" [{t['guard']}]" if t.get("guard") else ""
                    lines.append(
                        f"    Transition: {t['source']} → {t['target']}"
                        f"  trigger: {t.get('trigger', '')}{guard}"
                    )

    if model.get("interactions"):
        lines.append(f"\nSequence Diagrams / Interactions ({len(model['interactions'])}):")
        for ia in model["interactions"]:
            lines.append(f"  • {ia['name']}")
            if ia.get("lifelines"):
                ll_str = ", ".join(
                    f"{ll['name']} ({ll.get('represents', '')})"
                    for ll in ia["lifelines"]
                )
                lines.append(f"    Lifelines: {ll_str}")
            for msg in ia.get("messages", []):
                lines.append(
                    f"    Message: {msg.get('from', '?')} → {msg.get('to', '?')}"
                    f"  : {msg.get('name', '')}  [{msg.get('sort', '')}]"
                )

    if model.get("warnings"):
        lines.append(f"\nParse Warnings:")
        for w in model["warnings"]:
            lines.append(f"  ⚠ {w}")

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
        return jsonify({
            "model": model,
            "summary": summary_text,
            "format_info": model.get("format_info", {}),
            "warnings": model.get("warnings", []),
        })
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
