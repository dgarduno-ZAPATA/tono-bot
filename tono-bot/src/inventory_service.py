import csv
import os
import time
import requests
from io import StringIO

def _clean_price(value):
    if value is None:
        return ""
    s = str(value).strip()
    s2 = s.replace("$", "").replace(",", "").strip()
    return s2 if s2 else s

class InventoryService:
    def __init__(self, local_path: str, sheet_csv_url: str | None = None, refresh_seconds: int = 300):
        self.local_path = local_path
        self.sheet_csv_url = sheet_csv_url
        self.refresh_seconds = refresh_seconds
        self.items = []
        self._last_load_ts = 0

    def load(self, force: bool = False):
        now = time.time()
        if not force and self.items and (now - self._last_load_ts) < self.refresh_seconds:
            return

        rows = []
        if self.sheet_csv_url:
            r = requests.get(self.sheet_csv_url, timeout=20)
            r.raise_for_status()
            content = r.text
            f = StringIO(content)
            reader = csv.DictReader(f)
            rows = list(reader)
        else:
            if not os.path.exists(self.local_path):
                self.items = []
                return
            # fallback local
            with open(self.local_path, newline="", encoding="latin-1") as f:
                reader = csv.DictReader(f)
                rows = list(reader)

        normalized = []
        for r in rows:
            r = {(k or "").strip(): (v or "").strip() for k, v in r.items()}

            # status: Disponible/Vendido
            status = (r.get("status") or "").strip().lower()
            if status and status not in ["disponible", "available", "1", "si", "sí", "yes"]:
                continue

            item = {
                "Marca": r.get("Marca", "Foton"),
                "Modelo": r.get("Modelo", ""),
                "Año": r.get("Año", r.get("Anio", "")),
                "Precio": _clean_price(r.get("Precio", r.get("Precio Distribuidor", r.get(" Precio Distribuidor", "")))),
                "photos": r.get("photos", ""),
            }
            normalized.append(item)

        self.items = normalized
        self._last_load_ts = now

    def ensure_loaded(self):
        self.load(force=False)
