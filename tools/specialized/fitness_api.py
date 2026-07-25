"""Fitness & Nutrition API tool — wger exercise database + USDA FoodData Central.

Data sources (all free, no pip dependencies):
- wger (https://wger.de/api/v2/) — 690+ exercises with muscles, equipment, images
- USDA FoodData Central (https://api.nal.usda.gov/fdc/v1/) — 380K+ foods

Offline calculators (pure stdlib Python):
- BMI, TDEE (Mifflin-St Jeor), one-rep max (Epley/Brzycki/Lombardi),
  macro splits, body fat % (US Navy method)
"""

from __future__ import annotations

import math
from typing import Any

import httpx

from tools.base import Tool
from tools.models import ToolInput, ToolOutput

# ── API endpoints ──────────────────────────────────────────────────────────
WGER_BASE = "https://wger.de/api/v2"
USDA_BASE = "https://api.nal.usda.gov/fdc/v1"


class FitnessApiTool(Tool):
    """Search exercises (wger) and foods (USDA), compute body metrics."""

    name = "fitness_api"
    description = (
        "Fitness and nutrition assistant. Search 690+ exercises by muscle, "
        "equipment, or category via wger. Look up macros and calories for "
        "380,000+ foods via USDA FoodData Central. Compute BMI, TDEE, "
        "one-rep max, macro splits, and body fat. "
        "Use action='search_exercises' to find exercises, "
        "action='search_foods' to look up nutrition info, "
        "action='calculator' for body metric calculations."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["search_exercises", "search_foods", "calculator"],
                "description": "What to do.",
            },
            "query": {
                "type": "string",
                "description": "Search term (exercise name/muscle/category, or food name).",
            },
            "muscle": {
                "type": "string",
                "description": "Filter exercises by muscle (e.g. 'biceps', 'chest', 'quadriceps').",
            },
            "equipment": {
                "type": "string",
                "description": "Filter exercises by equipment (e.g. 'barbell', 'dumbbell', 'body weight').",
            },
            "category": {
                "type": "string",
                "description": "Filter exercises by category (e.g. 'strength', 'cardio', 'stretching').",
            },
            "calculator_type": {
                "type": "string",
                "enum": ["bmi", "tdee", "one_rep_max", "macros", "body_fat"],
                "description": "Which calculator to use.",
            },
            "weight_kg": {
                "type": "number",
                "description": "Body weight in kg (for calculators).",
            },
            "height_cm": {
                "type": "number",
                "description": "Height in cm (for BMI, TDEE, body fat).",
            },
            "age": {
                "type": "integer",
                "description": "Age in years (for TDEE).",
            },
            "sex": {
                "type": "string",
                "enum": ["male", "female"],
                "description": "Biological sex (for TDEE, body fat).",
            },
            "activity_level": {
                "type": "string",
                "enum": ["sedentary", "light", "moderate", "active", "very_active"],
                "description": "Activity level for TDEE (default: moderate).",
            },
            "weight_lbs": {
                "type": "number",
                "description": "Weight lifted in lbs (for one_rep_max).",
            },
            "reps": {
                "type": "integer",
                "description": "Number of reps performed (for one_rep_max).",
            },
            "neck_cm": {
                "type": "number",
                "description": "Neck circumference in cm (for US Navy body fat).",
            },
            "waist_cm": {
                "type": "number",
                "description": "Waist circumference in cm (for US Navy body fat).",
            },
            "hip_cm": {
                "type": "number",
                "description": "Hip circumference in cm (for US Navy body fat, female only).",
            },
            "goal": {
                "type": "string",
                "enum": ["lose", "maintain", "gain"],
                "description": "Goal for macro split calculation.",
            },
            "max_results": {
                "type": "integer",
                "description": "Max results to return (default 5).",
            },
        },
        "required": ["action"],
    }

    async def run(self, input: ToolInput) -> ToolOutput:
        action = input.params.get("action")
        if not action:
            return ToolOutput(success=False, error="Parameter 'action' is required.")

        try:
            if action == "search_exercises":
                data = await self._search_exercises(input.params)
            elif action == "search_foods":
                data = await self._search_foods(input.params)
            elif action == "calculator":
                data = self._calculate(input.params)
            else:
                return ToolOutput(success=False, error=f"Unknown action: {action}")
            return ToolOutput(success=True, data=data)
        except Exception as exc:
            return ToolOutput(success=False, error=str(exc))

    # ── Exercise search (wger) ─────────────────────────────────────────────

    async def _search_exercises(self, params: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=15) as client:
            # Use exerciseinfo endpoint — returns full data with translations
            url = f"{WGER_BASE}/exerciseinfo/"
            query_params: dict[str, Any] = {"format": "json", "language": "2", "limit": 10}

            if params.get("muscle"):
                muscles = await self._wger_muscles(client)
                match = [m for m in muscles if params["muscle"].lower() in m["name"].lower()]
                if match:
                    query_params["muscles"] = match[0]["id"]

            if params.get("equipment"):
                equips = await self._wger_equipment(client)
                match = [e for e in equips if params["equipment"].lower() in e["name"].lower()]
                if match:
                    query_params["equipment"] = match[0]["id"]

            if params.get("category"):
                cats = await self._wger_categories(client)
                match = [c for c in cats if params["category"].lower() in c["name"].lower()]
                if match:
                    query_params["category"] = match[0]["id"]

            resp = await client.get(url, params=query_params)
            resp.raise_for_status()
            data = resp.json()

            exercises = []
            for ex in data.get("results", [])[: params.get("max_results", 5)]:
                # Extract English name from translations
                name = ""
                for t in ex.get("translations", []):
                    if t.get("language") == 2:  # English
                        name = t.get("name", "")
                        break
                if not name and ex.get("translations"):
                    name = ex["translations"][0].get("name", "Unknown")

                exercises.append({
                    "name": name,
                    "muscles": [m.get("name_en") or m.get("name", "") for m in ex.get("muscles", [])],
                    "muscles_secondary": [
                        m.get("name_en") or m.get("name", "")
                        for m in ex.get("muscles_secondary", [])
                    ],
                    "equipment": [e.get("name", "") for e in ex.get("equipment", [])],
                    "category": ex.get("category", {}).get("name", "") if isinstance(ex.get("category"), dict) else "",
                    "images": [img.get("image", "") for img in ex.get("images", [])[:1]],
                })

            return {"source": "wger.de", "count": len(exercises), "exercises": exercises}

    async def _wger_muscles(self, client: httpx.AsyncClient) -> list[dict]:
        resp = await client.get(f"{WGER_BASE}/muscle/?format=json&language=2&limit=50")
        resp.raise_for_status()
        return resp.json().get("results", [])

    async def _wger_equipment(self, client: httpx.AsyncClient) -> list[dict]:
        resp = await client.get(f"{WGER_BASE}/equipment/?format=json&language=2&limit=50")
        resp.raise_for_status()
        return resp.json().get("results", [])

    async def _wger_categories(self, client: httpx.AsyncClient) -> list[dict]:
        resp = await client.get(f"{WGER_BASE}/exercisecategory/?format=json&limit=50")
        resp.raise_for_status()
        return resp.json().get("results", [])

    # ── Food search (USDA) ─────────────────────────────────────────────────

    async def _search_foods(self, params: dict[str, Any]) -> dict[str, Any]:
        query = params.get("query", "")
        if not query:
            return {"error": "query is required for food search"}

        from config.settings import settings

        api_key = getattr(settings, "usda_api_key", "") or "DEMO_KEY"

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{USDA_BASE}/foods/search",
                params={
                    "api_key": api_key,
                    "query": query,
                    "pageSize": params.get("max_results", 5),
                    "dataType": ["Foundation", "SR Legacy"],
                },
            )
            resp.raise_for_status()
            data = resp.json()

            foods = []
            for item in data.get("foods", []):
                nutrients = {
                    n["nutrientName"]: n.get("value", 0)
                    for n in item.get("foodNutrients", [])
                    if n.get("nutrientName")
                }
                foods.append({
                    "name": item.get("description", ""),
                    "brand": item.get("brandOwner", ""),
                    "fdc_id": item.get("fdcId"),
                    "calories": nutrients.get("Energy", 0),
                    "protein_g": nutrients.get("Protein", 0),
                    "fat_g": nutrients.get("Total lipid (fat)", 0),
                    "carbs_g": nutrients.get("Carbohydrate, by difference", 0),
                    "fiber_g": nutrients.get("Fiber, total dietary", 0),
                    "sugar_g": nutrients.get("Sugars, total including NLEA", 0),
                    "serving": item.get("servingSizeUnit", "") + " " + str(item.get("servingSize", "")),
                })

            return {"source": "USDA FoodData Central", "count": len(foods), "foods": foods}

    # ── Offline calculators ────────────────────────────────────────────────

    def _calculate(self, params: dict[str, Any]) -> dict[str, Any]:
        calc = params.get("calculator_type", "")
        if not calc:
            return {"error": "calculator_type is required"}

        if calc == "bmi":
            return self._calc_bmi(params)
        elif calc == "tdee":
            return self._calc_tdee(params)
        elif calc == "one_rep_max":
            return self._calc_orm(params)
        elif calc == "macros":
            return self._calc_macros(params)
        elif calc == "body_fat":
            return self._calc_body_fat(params)
        return {"error": f"Unknown calculator: {calc}"}

    def _calc_bmi(self, p: dict) -> dict:
        w = p.get("weight_kg")
        h = p.get("height_cm")
        if not w or not h:
            return {"error": "weight_kg and height_cm required"}
        bmi = w / ((h / 100) ** 2)
        if bmi < 18.5:
            cat = "Underweight"
        elif bmi < 25:
            cat = "Normal weight"
        elif bmi < 30:
            cat = "Overweight"
        else:
            cat = "Obese"
        return {"bmi": round(bmi, 1), "category": cat}

    def _calc_tdee(self, p: dict) -> dict:
        w = p.get("weight_kg")
        h = p.get("height_cm")
        age = p.get("age")
        sex = p.get("sex", "male")
        activity = p.get("activity_level", "moderate")
        if not all([w, h, age]):
            return {"error": "weight_kg, height_cm, and age required"}

        w_f, h_f, age_f = float(w), float(h), float(age)

        bmr = 10 * w_f + 6.25 * h_f - 5 * age_f + (5 if sex == "male" else -161)

        multipliers = {
            "sedentary": 1.2,
            "light": 1.375,
            "moderate": 1.55,
            "active": 1.725,
            "very_active": 1.9,
        }
        tdee = bmr * multipliers.get(activity, 1.55)
        return {
            "bmr": round(bmr),
            "tdee": round(tdee),
            "activity_level": activity,
            "lose_weight": round(tdee - 500),
            "gain_weight": round(tdee + 300),
        }

    def _calc_orm(self, p: dict) -> dict:
        w = p.get("weight_lbs")
        r = p.get("reps")
        if not w or not r:
            return {"error": "weight_lbs and reps required"}
        if r == 1:
            return {"one_rep_max_lbs": w, "epley": w, "brzycki": w, "lombardi": w}
        epley = w * (1 + r / 30)
        brzycki = w * (36 / (37 - r))
        lombardi = w * (r**0.1)
        return {
            "one_rep_max_lbs": round(epley, 1),
            "epley": round(epley, 1),
            "brzycki": round(brzycki, 1),
            "lombardi": round(lombardi, 1),
        }

    def _calc_macros(self, p: dict) -> dict:
        tdee = p.get("tdee") or p.get("calories")
        goal = p.get("goal", "maintain")
        if not tdee:
            return {"error": "tdee or calories required"}
        if goal == "lose":
            cal = tdee - 500
        elif goal == "gain":
            cal = tdee + 300
        else:
            cal = tdee
        protein = cal * 0.30 / 4  # 30% protein
        fat = cal * 0.25 / 9  # 25% fat
        carbs = cal * 0.45 / 4  # 45% carbs
        return {
            "calories": round(cal),
            "protein_g": round(protein),
            "fat_g": round(fat),
            "carbs_g": round(carbs),
            "goal": goal,
            "split": "30% protein / 25% fat / 45% carbs",
        }

    def _calc_body_fat(self, p: dict) -> dict:
        """US Navy method."""
        waist = p.get("waist_cm")
        neck = p.get("neck_cm")
        hip = p.get("hip_cm")
        height = p.get("height_cm")
        sex = p.get("sex", "male")
        if not all([waist, neck, height]):
            return {"error": "waist_cm, neck_cm, and height_cm required"}
        if sex == "male":
            bf = 495 / (1.0324 - 0.19077 * math.log10(waist - neck) + 0.15456 * math.log10(height)) - 450
        else:
            if not hip:
                return {"error": "hip_cm required for female body fat calculation"}
            bf = 495 / (1.29579 - 0.35004 * math.log10(waist + hip - neck) + 0.22100 * math.log10(height)) - 450
        return {"body_fat_percent": round(bf, 1), "method": "US Navy", "sex": sex}


def _strip_html(text: str) -> str:
    """Crude HTML tag stripper for wger descriptions."""
    import re
    clean = re.sub(r"<[^>]+>", "", text)
    return clean.strip()[:500]
