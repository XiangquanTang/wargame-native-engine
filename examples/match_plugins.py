"""Offline contract examples, NOT real LLM or JEV implementations.

python -m scripts.run_match ... --red llm --red-plugin examples.match_plugins:first_legal
python -m scripts.run_match ... --red llm-jev \
  --red-plugin examples.match_plugins:planner --red-evaluator examples.match_plugins:evaluator
"""
class FirstLegal:
    def __init__(self, config):
        self.config = config
    def reset(self, context):
        self.side = context.side
    def decide(self, context):
        return next((key for key, allowed in zip(context.binding.action_space.action_ids,
                                                context.binding.action_space.mask) if allowed), None)
    def close(self):
        pass

class Planner(FirstLegal):
    def propose(self, context):
        return [key for key, allowed in zip(context.binding.action_space.action_ids,
                                            context.binding.action_space.mask) if allowed][:8]

class Evaluator(FirstLegal):
    def evaluate(self, context, proposals):
        return proposals[0]


def first_legal(config):
    return FirstLegal(config)

def planner(config):
    return Planner(config)

def evaluator(config):
    return Evaluator(config)
