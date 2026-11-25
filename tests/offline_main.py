"""Load actual business functions without main's clients, DB or startup hooks.

This is a bounded rule-level harness, not a FastAPI integration test. Only
literal assignments and undecorated function definitions are evaluated.
External boundaries are fail-closed. Source can be set to a pristine baseline.
"""
import ast
import datetime
import json
import os
from pathlib import Path
import re
import typing
import unicodedata
from zoneinfo import ZoneInfo
from pydantic import BaseModel, Field


def forbidden(*args, **kwargs):
    raise AssertionError("External side effect attempted during offline test")


def load_main(source=None):
    path = Path(source or os.environ.get("WHAPSBOT_TEST_SOURCE", Path(__file__).parents[1] / "main.py"))
    tree = ast.parse(path.read_text(encoding="utf-8"))
    namespace = dict(vars(typing))
    namespace.update(datetime=datetime.datetime, timedelta=datetime.timedelta,
                     timezone=datetime.timezone, ZoneInfo=ZoneInfo,
                     re=re, json=json, unicodedata=unicodedata,
                     BaseModel=BaseModel, Field=Field, __name__="offline_rules",
                     LOCAL_TZ=ZoneInfo("America/Mexico_City"))
    nodes = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
    for node in tree.body:
        if isinstance(node, ast.Assign):
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    namespace[target.id] = value
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.decorator_list:
            if all(isinstance(d, ast.Constant) for d in node.args.defaults + [d for d in node.args.kw_defaults if d]):
                nodes.append(node)
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), str(path), "exec"), namespace)
    schemas = [node for node in tree.body if isinstance(node, ast.ClassDef)
               and node.name in {"AnalisisMensajeProspecto", "MemoriaHistoricaConversacion"}]
    exec(compile(ast.Module(body=schemas, type_ignores=[]), str(path), "exec"), namespace)
    for name in ("validar_zona_desconocida_con_google", "ejecutar_gemini_con_reintentos",
                 "enviar_respuesta_twilio", "enviar_alerta_admin_whatsapp",
                 "crear_tarea_admin_pendiente"):
        namespace[name] = forbidden
    # Read the real checked-in tariff; no credentials or network access.
    namespace["cargar_configuracion_precios"] = lambda: json.loads(
        (Path(__file__).parents[1] / "config/precios.json").read_text())
    namespace["obtener_anio_inicio_ciclo_escolar"] = lambda: 2026
    return namespace
