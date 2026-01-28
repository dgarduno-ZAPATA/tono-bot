import csv
import os
import time
import httpx
from io import StringIO

def _clean_price(value):
    if value is None:
        return ""
    s = str(value).strip()
    s2 = s.replace("$", "").replace(",", "").strip()
    return s2 if s2 else s

def _clean_cell(v):
    # convierte cualquier cosa (incluidas listas) a texto
    return (str(v) if v is not None else "").strip()

class InventoryService:
    def __init__(self, local_path: str, sheet_csv_url: str | None = None, refresh_seconds: int = 300):
        self.local_path = local_path
        self.sheet_csv_url = sheet_csv_url
        self.refresh_seconds = refresh_seconds
        self.items = []
        self._last_load_ts = 0

    async def load(self, force: bool = False):
        now = time.time()
        if not force and self.items and (now - self._last_load_ts) < self.refresh_seconds:
            return

        rows = []
        if self.sheet_csv_url:
            url = (self.sheet_csv_url or "").strip()
            async with httpx.AsyncClient(timeout=20.0) as client:
                r = await client.get(url)
            r.raise_for_status()
            content = r.text

            f = StringIO(content)
            reader = csv.DictReader(f)
            rows = list(reader)
        else:
            if not os.path.exists(self.local_path):
                self.items = []
                return
            with open(self.local_path, newline="", encoding="latin-1") as f:
                reader = csv.DictReader(f)
                rows = list(reader)

        normalized = []
        for row in rows:
            # limpia headers/valores (a prueba de listas)
            row = { _clean_cell(k): _clean_cell(v) for k, v in (row or {}).items() }

            status = (row.get("status", "") or "").strip().lower()
            if status and status not in ["disponible", "available", "1", "si", "sí", "yes"]:
                continue

            item = {
                "Marca": row.get("Marca", "Foton"),
                "Modelo": row.get("Modelo", ""),
                "Año": row.get("Año", row.get("Anio", "")),
                "Precio": _clean_price(row.get("Precio", row.get("Precio Distribuidor", row.get(" Precio Distribuidor", "")))),
                "photos": row.get("photos", ""),
            }
            normalized.append(item)

        self.items = normalized
        self._last_load_ts = now

    async def ensure_loaded(self):
        await self.load(force=False)

