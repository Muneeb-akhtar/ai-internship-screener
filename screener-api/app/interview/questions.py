"""Question bank: every candidate for the same role gets EXACTLY the same questions.

The voice assistant's prompt has placeholders ({{technical_question}}, {{problem_question}});
the interview page fills them from here via Vapi `variableValues`. Keeping the questions in
code (not in the LLM's imagination) is what makes the screening fair and comparable.
Add roles by adding entries - keys are matched as lowercase keywords in the role name.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class RoleQuestions:
    technical: str
    problem: str


QUESTION_BANK: dict[str, RoleQuestions] = {
    "python": RoleQuestions(
        technical="What is the difference between a list and a tuple in Python, and when would you use each?",
        problem="How would you find duplicate email addresses in a list of ten thousand customers?",
    ),
    "data": RoleQuestions(
        technical="What is the difference between the mean and the median, and when is the median more useful?",
        problem="A sales report suddenly shows twice the usual revenue for one day. How would you check whether it's real or a data error?",
    ),
    "web": RoleQuestions(
        technical="What is the difference between HTML, CSS and JavaScript in a web page?",
        problem="A button on a website works on desktop but not on mobile. How would you find the cause?",
    ),
    "marketing": RoleQuestions(
        technical="What is the difference between reach and engagement on social media?",
        problem="An Instagram post got many likes but almost no website visits. What would you look at to improve that?",
    ),
    "design": RoleQuestions(
        technical="What makes a mobile app screen easy to use? Give two principles.",
        problem="Users say a sign-up form is confusing. How would you find out why and fix it?",
    ),
}

GENERIC = RoleQuestions(
    technical="Tell me about a tool, skill or subject from your studies that is relevant to this role, and how you have used it.",
    problem="Imagine you are given a task with an unclear deadline and unclear instructions. What steps would you take?",
)


def interview_variables(candidate_name: str, role: str, settings) -> dict[str, str]:  # noqa: ANN001
    """Values for the {{placeholders}} in vapi/assistant-prompt.md - identical for the
    browser call (Vapi variableValues) and the WhatsApp call (ElevenLabs dynamic_variables)."""
    questions = questions_for_role(role)
    return {
        "candidate_name": candidate_name.split()[0] if candidate_name.strip() else "there",
        "role": role,
        "company_name": settings.company_name,
        "technical_question": questions.technical,
        "problem_question": questions.problem,
        "internship_duration": settings.internship_duration,
        "internship_schedule": settings.internship_schedule,
        "internship_start": settings.internship_start,
        "internship_faq": settings.internship_faq,
    }


def questions_for_role(role: str) -> RoleQuestions:
    """First keyword found in the role name wins, e.g. 'Python Developer Intern' -> python."""
    role_l = role.lower()
    for keyword, questions in QUESTION_BANK.items():
        if keyword in role_l:
            return questions
    return GENERIC
