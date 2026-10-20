"""Adapt bound persistent actions to the existing operation lifecycle tracker."""
from wargame_native_engine.agents import SeatDecision, SeatOperation
from wargame_native_engine.operations.execution_lifecycle import OperationExecutionTracker


class BoundExecutionTracker:
    def __init__(self, episode_id, side):
        self.side = side
        self.tracker = OperationExecutionTracker(episode_id)
        self.latest = ()

    def submit(self, actions, view):
        operations, correlations = [], []
        for record in actions:
            action = record['action']
            kind, actor, params = action['action_type'], action['actor_id'], action['parameters']
            kwargs = {}
            if kind == 'MOVE':
                kwargs['destination_hex'] = params['path'][-1]
            elif kind in {'BOARD', 'UNLOAD'}:
                kwargs['target_id'] = action['target_id']
            elif kind == 'FIRE' and params.get('fire_mode') == 'INDIRECT':
                kind = 'INDIRECT_FIRE'
                kwargs.update(fire_mode='INDIRECT', shooter_id=actor,
                              weapon_id=params['weapon_id'], target_hex=params['target_hex'])
            else:
                continue  # instantaneous commands have action-result evidence, no persistent handle
            correlations.append(dict(operation_index=len(operations), actor_id=actor,
                                     accepted_for_preview=True, action_id=record['action_id']))
            operations.append(SeatOperation(kind, (actor,), **kwargs))
        self.latest = self.tracker.submit(self.side, SeatDecision(tuple(operations)),
            decision_window_index=view.step_index, frame_index=view.frame_index,
            sim_time_ms=view.sim_time_ms, command_status='SUBMITTED')
        self.tracker.bind_plan(self.latest, correlations)

    def consume(self, fact_batch):
        frames = fact_batch.get('frames', ())
        results = {str(r.get('action_id')): (r, f) for f in frames for r in f.get('action_results', ())}
        for handle in self.latest:
            record = self.tracker.get(self.side, handle)
            result = results.get(record.action_id)
            if result is None:
                self.tracker.set_command_status((handle,), 'APPLICATION_EVIDENCE_UNKNOWN', reason_code='ACTION_RESULT_NOT_RETAINED')
            else:
                action_result, frame = result
                self.tracker.set_command_status((handle,),
                    'APPLIED' if action_result.get('accepted') is True else 'REJECTED_AT_APPLY',
                    frame_index=frame['frame_index'], sim_time_ms=frame['sim_time_ms'])
        for frame in frames:
            self.tracker.consume(self.side, frame)
