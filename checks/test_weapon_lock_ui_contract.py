"""Concrete weapon binding survives UI/tool/agent/projection boundaries."""
import unittest
from pathlib import Path
from wargame_native_engine.agents.seat import SeatOperation
from wargame_native_engine.agents.random_seat import _product_operation
from wargame_native_engine.agents.tool_calling import function_schemas
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.io.scenario import load_scenario
from wargame_native_engine.projection.operator_lifecycle import project_operator_lifecycle, WeaponPosture
from wargame_native_engine.projection.decision_semantic import DecisionCapabilityFamily
from wargame_native_engine.training.decision_encoder import (CAPABILITY_FAMILY_ORDER,
    DECISION_ACTION_ENCODER_SCHEMA_VERSION, DECISION_OBSERVATION_ENCODER_SCHEMA_VERSION,
    DECISION_OBSERVATION_ENCODER_V2_SCHEMA_VERSION)

class WeaponLockUIContractTests(unittest.TestCase):
    def test_seat_preserves_lock_choice(self):
        payload = SeatOperation('WEAPON_LOCK', (1,), weapon_choices=((1,36),)).preview_arguments()
        self.assertEqual(payload['weapon_choices'], {1:36})
        with self.assertRaises(ValueError):
            SeatOperation('WEAPON_UNLOCK', (1,), weapon_choices=((1,36),))

    def test_random_agent_does_not_drop_binding(self):
        action = {'side':'red','actor_id':1,'action_type':'WEAPON_LOCK','parameters':{'weapon_id':36}}
        kwargs = dict(expected_side='red',allowed_types={'WEAPON_LOCK'},controllable_actor_ids={1})
        self.assertEqual(_product_operation(action, **kwargs).weapon_choices, ((1,36),))
        for invalid in (None, True, '36'):
            action['parameters']['weapon_id']=invalid
            self.assertIsNone(_product_operation(action, **kwargs))

    def test_external_adapter_never_guesses_lock_weapon(self):
        from wargame_native_engine.agents.miaosuan import _candidate, _matches, _numeric_type
        from wargame_native_engine.core.action import EngineAction, ActionType
        action=EngineAction('red',1,ActionType.WEAPON_LOCK,parameters={'weapon_id':36})
        self.assertEqual(_candidate(action), {'weapon_id':36})
        self.assertFalse(_matches({'type':11,'obj_id':1},action))
        self.assertFalse(_matches({'type':11,'obj_id':1,'weapon_id':5},action))
        self.assertTrue(_matches({'type':11,'obj_id':1,'weapon_id':36},action))
        self.assertIsNone(_numeric_type(EngineAction('red',1,ActionType.WEAPON_UNLOCK)))

    def test_tool_schema_explains_binding_and_exposes_unlock(self):
        schema = next(s for s in function_schemas() if s['name']=='preview_game_operation')
        props=schema['parameters']['properties']['operation']['properties']
        self.assertIn('WEAPON_UNLOCK', props['operation_type']['enum'])
        self.assertIn('WEAPON_LOCK', props['weapon_choices']['description'])

    def test_folded_unlocked_posture_is_valid_and_requires_unfold(self):
        engine=NativeEngine(load_scenario(Path(__file__).resolve().parents[1]/'examples/maps/scenarios/direct-fire.json'))
        self.addCleanup(engine.close)
        engine.reset(seed=42)
        actor=engine._world.operators[1]
        actor.weapon_locked=False;actor.locked_weapon_id=None;actor.weapon_unfolded=False
        view=project_operator_lifecycle(engine._world,1)
        self.assertEqual(view.weapon_lifecycle.posture,WeaponPosture.FOLDED)
        self.assertNotIn('WEAPON_POSTURE_INCONSISTENT',view.integrity.codes)

    def test_inventory_tensor_distinguishes_locked_weapon_without_targets(self):
        from wargame_native_engine.training.decision_encoder import (
            _encode_weapon_inventory, DecisionEncoderV2Config, INVENTORY_UNIT_FEATURES,
            DecisionEncodingError)
        weapons=[{'weapon_id':wid,'resource_policy':'UNKNOWN','attack_modes':[],
                  'shared_with_weapon_ids':[]} for wid in (36,54)]
        def encode(locked):
            return _encode_weapon_inventory([{'operator_id':1,'weapons':weapons,
                'ammunition_pools':{},'locked_weapon_id':locked}],DecisionEncoderV2Config())[0][0]
        first,second,unlocked=encode(36),encode(54),encode(None)
        present=INVENTORY_UNIT_FEATURES.index('locked_weapon_present')
        reference=INVENTORY_UNIT_FEATURES.index('locked_weapon_reference')
        self.assertNotEqual(first,second)
        self.assertEqual((first[present],first[reference]),(1,36))
        self.assertEqual((second[present],second[reference]),(1,54))
        self.assertEqual((unlocked[present],unlocked[reference]),(0,0))
        for invalid in (True, '36', 999):
            with self.assertRaises(DecisionEncodingError): encode(invalid)

    def test_encoder_contract_changes_are_explicit(self):
        self.assertIn(DecisionCapabilityFamily.WEAPON_UNLOCK,CAPABILITY_FAMILY_ORDER)
        self.assertTrue(DECISION_ACTION_ENCODER_SCHEMA_VERSION.endswith('/1.1'))
        self.assertTrue(DECISION_OBSERVATION_ENCODER_SCHEMA_VERSION.endswith('/1.1'))
        self.assertTrue(DECISION_OBSERVATION_ENCODER_V2_SCHEMA_VERSION.endswith('/2.1'))
