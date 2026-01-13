import csv
import os

class InventoryService:
    def __init__(self, path: str):
        self.path = path
        self.items = []

    def load(self):
        # Si el archivo no existe, dejamos inventario vacío
        if not os.path.exists(self.path):
            self.items = []
            return

        with open(self.path, newline="", encoding="latin-1") as f:
            reader = csv.DictReader(f)
            self.items = list(reader)

    def search(self, vehicle_category=None, condition=None, limit: int = 3):
        results = self.items

        if vehicle_category:
            results = [i for i in results if (i.get("vehicle_category") or "").strip().lower() == vehicle_category.lower()]

        if condition:
            results = [i for i in results if (i.get("condition") or "").strip().lower() == condition.lower()]

        return results[:limit]

