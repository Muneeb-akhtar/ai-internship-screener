"""Wires the nodes into a LangGraph StateGraph.

KEY CONCEPT - Edges:
  add_edge(a, b)                    -> after a, always run b
  add_conditional_edges(a, router)  -> after a, run whatever node router(state) names
  START / END                       -> special entry and exit points

KEY CONCEPT - Checkpointer:
  compile(checkpointer=...) makes the graph save its state after every node, keyed
  by `thread_id` (we use one thread per evaluation). That gives us:
    - pause/resume for human review (interrupt)  - crash recovery
    - a full history of every step for auditing
  Production uses AsyncPostgresSaver; tests use the in-memory saver.

The flow (also rendered by `python -m app.agent.graph`):

  START -> check_transcript -> extract_answers -> score -> self_check
                 |                   |             ^          |
                 v                   v             +-(retry)--+
             incomplete          incomplete                   |
                                                +-------------+-------------+
                                                v             v             v
                                             reject   request_review    shortlist
                                                              v
                                                         human_review (interrupt)
                                                       APPROVE |  REJECT
                                                    shortlist <+> reject
"""

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agent.nodes import (
    ScreeningNodes,
    route_after_check,
    route_after_extract,
    route_after_review,
    route_after_transcript_check,
)
from app.agent.state import ScreeningState


def build_graph(
    nodes: ScreeningNodes, checkpointer: BaseCheckpointSaver | None = None
) -> CompiledStateGraph:
    g = StateGraph(ScreeningState)

    # 1) Register nodes (name -> function)
    g.add_node("check_transcript", nodes.check_transcript)
    g.add_node("extract_answers", nodes.extract_answers)
    g.add_node("incomplete", nodes.incomplete)
    g.add_node("score", nodes.score)
    g.add_node("self_check", nodes.self_check)
    g.add_node("request_review", nodes.request_review)
    g.add_node("human_review", nodes.human_review)
    g.add_node("shortlist", nodes.shortlist)
    g.add_node("reject", nodes.reject)

    # 2) Connect them. The list after each router names its possible targets,
    #    which lets LangGraph validate the graph and draw the diagram.
    g.add_edge(START, "check_transcript")
    g.add_conditional_edges(
        "check_transcript", route_after_transcript_check, ["extract_answers", "incomplete"]
    )
    g.add_conditional_edges("extract_answers", route_after_extract, ["score", "incomplete"])
    g.add_edge("score", "self_check")
    g.add_conditional_edges(
        "self_check", route_after_check, ["score", "reject", "request_review", "shortlist"]
    )
    g.add_edge("request_review", "human_review")
    g.add_conditional_edges("human_review", route_after_review, ["shortlist", "reject"])
    g.add_edge("incomplete", END)
    g.add_edge("shortlist", END)
    g.add_edge("reject", END)

    # 3) Compile: validates the wiring and attaches the checkpointer
    return g.compile(checkpointer=checkpointer, name="screening_decision_agent")


if __name__ == "__main__":
    # Print a Mermaid diagram of the graph (paste into README / mermaid.live):
    #   python -m app.agent.graph
    from unittest.mock import MagicMock

    print(build_graph(ScreeningNodes(MagicMock(), MagicMock())).get_graph().draw_mermaid())
