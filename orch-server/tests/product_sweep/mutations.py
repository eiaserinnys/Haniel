"""Build the P8-1 counterfactual from the current production definition."""

import ast
import inspect
from functools import lru_cache

from haniel_orch.statechart import definitions


@lru_cache(maxsize=1)
def without_p81():
    tree = ast.parse(inspect.getsource(definitions.OrchRequest))
    cls = tree.body[0]
    cls.name = "OrchRequestWithoutP81"
    found = set()
    statuses = ("pending", "again", "retry", "deploying", "success", "rejected", "superseded")
    for index, statement in enumerate(cls.body):
        if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
            continue
        target = statement.targets[0]
        if not isinstance(target, ast.Name) or target.id not in {"late_failed", "node_failed"}:
            continue
        expressions = []
        for status in statuses:
            region = "open_" if status in {"pending", "again", "retry", "deploying"} else "closed"
            action = 'on="ack"' if target.id == "node_failed" else ""
            expressions.append(f"req.status.{region}.{status}.to.itself({action})")
        replacement = ast.parse(f"{target.id} = (" + " | ".join(expressions) + ")").body[0]
        cls.body[index] = ast.copy_location(replacement, statement)
        found.add(target.id)
    assert found == {"late_failed", "node_failed"}
    namespace = dict(vars(definitions))
    exec(compile(ast.fix_missing_locations(tree), "<p81 counterfactual>", "exec"), namespace)
    mutant = namespace[cls.name]
    definitions.REQUIRED_FACTS[mutant] = definitions.REQUIRED_FACTS[definitions.OrchRequest]
    return mutant
