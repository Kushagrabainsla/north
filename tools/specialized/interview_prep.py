"""Interview Preparation tool — company research, STAR stories, salary prep.

Ported from the Hermes 'interview' skill. Provides structured workflows
for job interview preparation: company research, story building, salary
negotiation prep, and follow-up drafting.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any

from tools.base import Tool
from tools.models import ToolInput, ToolOutput

# ── Data directory ─────────────────────────────────────────────────────────
INTERVIEW_DIR = os.path.expanduser("~/.north/interview")


def _ensure_dir() -> None:
    os.makedirs(INTERVIEW_DIR, exist_ok=True)


def _load_json(filename: str) -> dict:
    path = os.path.join(INTERVIEW_DIR, filename)
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def _save_json(filename: str, data: dict) -> None:
    _ensure_dir()
    path = os.path.join(INTERVIEW_DIR, filename)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


class InterviewPrepTool(Tool):
    """Interview preparation: company research, STAR stories, salary strategy."""

    name = "interview_prep"
    description = (
        "Interview preparation system. Use action='research_company' to generate "
        "a research brief for a company, action='build_story' to create a STAR-format "
        "story from your experience, action='salary_prep' for salary negotiation "
        "strategy, action='draft_followup' for thank-you emails, or "
        "action='list_stories' / 'list_research' to see saved items."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "research_company",
                    "build_story",
                    "salary_prep",
                    "draft_followup",
                    "list_stories",
                    "list_research",
                ],
                "description": "What to do.",
            },
            "company": {
                "type": "string",
                "description": "Company name (for research_company).",
            },
            "role": {
                "type": "string",
                "description": "Job role you're interviewing for.",
            },
            "situation": {
                "type": "string",
                "description": "The situation/challenge (for build_story).",
            },
            "task": {
                "type": "string",
                "description": "Your specific task (for build_story).",
            },
            "action_taken": {
                "type": "string",
                "description": "What action you took (for build_story).",
            },
            "result": {
                "type": "string",
                "description": "The outcome (for build_story).",
            },
            "lesson": {
                "type": "string",
                "description": "What you learned (for build_story).",
            },
            "location": {
                "type": "string",
                "description": "Job location (for salary_prep).",
            },
            "tone": {
                "type": "string",
                "description": "Tone for follow-up email (professional, friendly, warm).",
            },
            "interview_notes": {
                "type": "string",
                "description": "Notes from the interview (for draft_followup).",
            },
        },
        "required": ["action"],
    }

    def format_output(self, data: dict[str, Any]) -> str:
        if "error" in data:
            return f"Error: {data['error']}"
        if "story" in data:
            s = data["story"]
            lines = [f"Story: {s.get('id', '')}", ""]
            for key in ["situation", "task", "action", "result", "lesson"]:
                if s.get(key):
                    lines.append(f"  {key.upper()}: {s[key]}")
            return "\n".join(lines)
        if "brief" in data:
            return data["brief"]
        if "strategy" in data:
            return data["strategy"]
        if "email" in data:
            return data["email"]
        return json.dumps(data, indent=2)

    async def run(self, input: ToolInput) -> ToolOutput:
        action = input.params.get("action")
        if not action:
            return ToolOutput(success=False, error="Parameter 'action' is required.")

        try:
            if action == "research_company":
                data = self._research_company(input.params)
            elif action == "build_story":
                data = self._build_story(input.params)
            elif action == "salary_prep":
                data = self._salary_prep(input.params)
            elif action == "draft_followup":
                data = self._draft_followup(input.params)
            elif action == "list_stories":
                data = self._list_stories()
            elif action == "list_research":
                data = self._list_research()
            else:
                return ToolOutput(success=False, error=f"Unknown action: {action}")
            return ToolOutput(success=True, data=data)
        except Exception as exc:
            return ToolOutput(success=False, error=str(exc))

    def _research_company(self, params: dict) -> dict:
        """Generate a structured company research framework."""
        company = params.get("company", "")
        role = params.get("role", "")
        if not company:
            return {"error": "company is required"}

        brief = f"🔍 COMPANY RESEARCH BRIEF: {company}\n"
        brief += "=" * 60 + "\n"
        brief += f"Role: {role}\n\n"

        brief += "RESEARCH FRAMEWORK:\n"
        brief += "-" * 40 + "\n\n"

        brief += "1. COMPANY BASICS\n"
        brief += "   • What does the company do?\n"
        brief += "   • How do they make money?\n"
        brief += "   • Who are their customers?\n"
        brief += "   • What is their market position?\n\n"

        brief += "2. RECENT NEWS\n"
        brief += "   • Recent product launches\n"
        brief += "   • Funding rounds or acquisitions\n"
        brief += "   • Leadership changes\n"
        brief += "   • Strategic pivots\n\n"

        brief += "3. CULTURE & VALUES\n"
        brief += "   • Mission statement\n"
        brief += "   • Core values\n"
        brief += "   • Work environment (Glassdoor/LinkedIn)\n"
        brief += "   • Recent employee sentiment\n\n"

        brief += "4. TECH STACK & CHALLENGES\n"
        brief += "   • Engineering blog posts\n"
        brief += "   • Open source contributions\n"
        brief += "   • Known technical challenges\n"
        brief += "   • Recent engineering hires\n\n"

        brief += "5. COMPETITIVE LANDSCAPE\n"
        brief += "   • Main competitors\n"
        brief += "   • Differentiators\n"
        brief += "   • Market trends\n\n"

        brief += f"6. TALKING POINTS FOR {role.upper()}\n"
        brief += "   • How your skills map to their needs\n"
        brief += "   • Specific projects/products you can reference\n"
        brief += "   • Questions to ask the interviewer\n"

        # Save research
        research = _load_json("research.json")
        if "companies" not in research:
            research["companies"] = []
        research["companies"].append({
            "company": company,
            "role": role,
            "created_at": datetime.now().isoformat(),
            "status": "framework_generated",
        })
        _save_json("research.json", research)

        return {"brief": brief, "company": company, "role": role}

    def _build_story(self, params: dict) -> dict:
        """Build a STAR-format interview story."""
        situation = params.get("situation", "")
        if not situation:
            return {"error": "situation is required"}

        story_id = f"STORY-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        story = {
            "id": story_id,
            "situation": situation,
            "task": params.get("task", ""),
            "action": params.get("action_taken", ""),
            "result": params.get("result", ""),
            "lesson": params.get("lesson", ""),
            "created_at": datetime.now().isoformat(),
        }

        # Save story
        data = _load_json("stories.json")
        if "stories" not in data:
            data["stories"] = []
        data["stories"].append(story)
        _save_json("stories.json", data)

        # Build response
        response = {"story": story}

        # Add guidance if fields are missing
        missing = []
        if not story["task"]:
            missing.append("task (What was your specific responsibility?)")
        if not story["action"]:
            missing.append("action_taken (What did you specifically do?)")
        if not story["result"]:
            missing.append("result (What was the measurable outcome?)")

        if missing:
            response["hint"] = f"Consider adding: {', '.join(missing)}"

        return response

    def _salary_prep(self, params: dict) -> dict:
        """Generate salary negotiation strategy."""
        role = params.get("role", "the position")
        location = params.get("location", "")

        strategy = "💰 SALARY NEGOTIATION STRATEGY\n"
        strategy += "=" * 50 + "\n\n"
        strategy += f"Role: {role}\n"
        if location:
            strategy += f"Location: {location}\n"
        strategy += "\n"

        strategy += "BEFORE THE INTERVIEW:\n"
        strategy += "-" * 30 + "\n"
        strategy += "• Research market rates on Levels.fyi, Glassdoor, Blind\n"
        strategy += "• Know your BATNA (Best Alternative To Negotiated Agreement)\n"
        strategy += "• Set your range: walk-away number → target → stretch\n"
        strategy += "• Consider total comp: base, bonus, equity, benefits\n\n"

        strategy += "DURING NEGOTIATION:\n"
        strategy += "-" * 30 + "\n"
        strategy += "• Let THEM name a number first if possible\n"
        strategy += "• If pressed: 'Based on my research and experience, I'm targeting $X-$Y'\n"
        strategy += "• Anchor high but reasonable (top of market for your level)\n"
        strategy += "• Negotiate the full package, not just base salary\n"
        strategy += "• Use silence — don't fill pauses\n\n"

        strategy += "POWER PHRASES:\n"
        strategy += "-" * 30 + "\n"
        strategy += '• "I\'m excited about this role and want to find a number that works for both of us."\n'
        strategy += '• "Based on my research, the market rate for this role is $X-$Y."\n'
        strategy += '• "I have another offer at $X, but I prefer your company."\n'
        strategy += '• "Can you help me understand how you arrived at that number?"\n\n'

        strategy += "COMMON MISTAKES:\n"
        strategy += "-" * 30 + "\n"
        strategy += "• Never give a number first if you can avoid it\n"
        strategy += "• Don't apologize for negotiating\n"
        strategy += "• Don't accept on the spot — ask for 24-48 hours\n"
        strategy += "• Don't lie about competing offers"

        return {"strategy": strategy, "role": role, "location": location}

    def _draft_followup(self, params: dict) -> dict:
        """Draft a post-interview thank-you email."""
        company = params.get("company", "your company")
        role = params.get("role", "")
        notes = params.get("interview_notes", "")

        email = f"Subject: Thank you for the {role} interview\n\n"
        email += "Dear [Interviewer Name],\n\n"
        email += "Thank you for taking the time to speak with me about the "
        email += f"{role} position at {company}. "
        email += "I enjoyed learning about the team and the exciting work you're doing.\n\n"

        if notes:
            email += f"Based on our conversation, I'm particularly excited about {notes}. "
        else:
            email += "I'm particularly excited about the opportunity to contribute to your team. "

        email += "I believe my experience in [relevant skill] would allow me to "
        email += "make a meaningful impact from day one.\n\n"

        email += "Please don't hesitate to reach out if you need any additional "
        email += "information. I look forward to hearing about next steps.\n\n"
        email += "Best regards,\n[Your Name]"

        # Save
        data = _load_json("followups.json")
        if "emails" not in data:
            data["emails"] = []
        data["emails"].append({
            "company": company,
            "role": role,
            "drafted_at": datetime.now().isoformat(),
        })
        _save_json("followups.json", data)

        return {"email": email, "company": company, "role": role}

    def _list_stories(self) -> dict:
        data = _load_json("stories.json")
        stories = data.get("stories", [])
        return {
            "count": len(stories),
            "stories": [
                {"id": s["id"], "situation": s["situation"][:100], "created_at": s.get("created_at", "")}
                for s in stories
            ],
        }

    def _list_research(self) -> dict:
        data = _load_json("research.json")
        companies = data.get("companies", [])
        return {
            "count": len(companies),
            "companies": [
                {"company": c["company"], "role": c.get("role", ""), "created_at": c.get("created_at", "")}
                for c in companies
            ],
        }
