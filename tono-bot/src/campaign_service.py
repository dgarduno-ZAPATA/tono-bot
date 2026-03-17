"""
Campaign Service — Lee campañas activas desde Google Sheets.

El Sheet tiene 5 columnas:
  Activa | Tracking ID | Keywords | Campaña | Instrucciones

El bot lee el Sheet periódicamente, filtra las activas,
y genera bloques de texto para inyectar en el System Prompt.
"""

import csv
import logging
import time
from io import StringIO
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)


class Campaign:
    """Representa una campaña activa del Sheet."""

    def __init__(self, row: Dict[str, str]):
        self.active = (row.get("Activa", "") or "").strip().upper() == "SI"
        self.tracking_id = (row.get("Tracking ID", "") or "").strip()
        self.keywords = [
            k.strip().lower()
            for k in (row.get("Keywords", "") or "").split(",")
            if k.strip()
        ]
        self.name = (row.get("Campaña", row.get("Campana", "")) or "").strip()
        self.instructions = (row.get("Instrucciones", "") or "").strip()

    def is_valid(self) -> bool:
        """Una campaña es válida si está activa y tiene instrucciones."""
        return self.active and bool(self.instructions)


class CampaignService:
    """Lee y cachea campañas activas desde Google Sheets CSV."""

    def __init__(self, csv_url: Optional[str] = None, refresh_seconds: int = 300):
        self.csv_url = (csv_url or "").strip() or None
        self.refresh_seconds = refresh_seconds
        self.campaigns: List[Campaign] = []
        self._last_load_ts: float = 0

    async def load(self, force: bool = False) -> None:
        """Carga campañas desde el Sheet CSV."""
        if not self.csv_url:
            self.campaigns = []
            return

        now = time.time()
        if not force and self.campaigns is not None and (now - self._last_load_ts) < self.refresh_seconds:
            return

        try:
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
                r = await client.get(self.csv_url)
            r.raise_for_status()

            reader = csv.DictReader(StringIO(r.text))
            loaded = []
            for row in reader:
                # Limpiar keys/values
                cleaned = {
                    (str(k) if k else "").strip(): (str(v) if v else "").strip()
                    for k, v in row.items()
                }
                campaign = Campaign(cleaned)
                if campaign.is_valid():
                    loaded.append(campaign)

            self.campaigns = loaded
            self._last_load_ts = now
            logger.info(f"📢 Campañas cargadas: {len(loaded)} activas de {sum(1 for _ in reader) + len(loaded)} totales")

        except Exception as e:
            logger.error(f"⚠️ Error cargando campañas: {e}")
            # Mantener campañas anteriores en caso de error de red

    async def ensure_loaded(self) -> None:
        """Asegura que las campañas estén cargadas (usa cache)."""
        await self.load(force=False)

    def get_active_campaigns(self) -> List[Campaign]:
        """Retorna solo campañas activas y válidas."""
        return [c for c in self.campaigns if c.is_valid()]

    def find_campaign_by_tracking_id(self, tracking_id: str) -> Optional[Campaign]:
        """Busca campaña por tracking ID."""
        if not tracking_id:
            return None
        tid = tracking_id.strip().upper()
        for c in self.get_active_campaigns():
            if c.tracking_id.upper() == tid:
                return c
        return None

    def find_campaign_by_keywords(self, message: str) -> Optional[Campaign]:
        """Busca campaña que coincida con keywords en el mensaje."""
        if not message:
            return None
        msg_lower = message.lower()
        for c in self.get_active_campaigns():
            if c.keywords and any(kw in msg_lower for kw in c.keywords):
                return c
        return None

    def build_campaigns_prompt_block(self) -> str:
        """
        Genera el bloque de texto de campañas para inyectar en el System Prompt.
        Cada campaña activa se convierte en una regla temporal.
        """
        active = self.get_active_campaigns()
        if not active:
            return ""

        blocks = []
        for c in active:
            block = (
                f'*** CAMPAÑA ACTIVA: "{c.name}" ***\n'
            )
            if c.tracking_id:
                block += f"TRACKING ID: {c.tracking_id}\n"
            if c.keywords:
                block += f"KEYWORDS DE DETECCIÓN: {', '.join(c.keywords)}\n"
            block += (
                f"\nINSTRUCCIONES:\n"
                f"{c.instructions}\n"
                f"*** FIN CAMPAÑA: {c.name} ***"
            )
            blocks.append(block)

        header = (
            "=== CAMPAÑAS ACTIVAS ===\n"
            "Las siguientes campañas están ACTIVAS. Si un cliente llega por el Tracking ID "
            "o menciona las keywords de alguna campaña, SIGUE LAS INSTRUCCIONES de esa campaña "
            "con PRIORIDAD sobre las reglas generales (inventario, financiamiento, PDFs).\n"
            "Si el cliente NO está relacionado con ninguna campaña, ignora este bloque.\n\n"
        )

        return header + "\n\n".join(blocks) + "\n=== FIN CAMPAÑAS ACTIVAS ==="
