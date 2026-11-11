#!/usr/bin/env python3
"""Offline, single-generation Qwen3-4B-Instruct-2507 HTTP service (Python 3.10)."""
import argparse
import gc
import json
import os
from pathlib import Path
import socket
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

MAX_BODY = 131072
MAX_MESSAGES = 64
MAX_TEXT = 32768
MAX_OUTPUT = 1024


class RequestError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message
        super().__init__(message)


def validate_request(data):
    if not isinstance(data, dict) or set(data) - {'messages', 'max_new_tokens'}:
        raise RequestError(400, 'invalid_request', 'Only messages and max_new_tokens are supported.')
    messages = data.get('messages')
    if not isinstance(messages, list) or not 1 <= len(messages) <= MAX_MESSAGES:
        raise RequestError(400, 'invalid_messages', 'messages must contain 1 to 64 items.')
    for message in messages:
        if (not isinstance(message, dict) or set(message) != {'role', 'content'}
                or message['role'] not in ('system', 'user', 'assistant')
                or not isinstance(message['content'], str)
                or not message['content'].strip() or len(message['content']) > MAX_TEXT):
            raise RequestError(400, 'invalid_messages', 'Each message needs a supported role and 1 to 32768 content characters.')
    if messages[-1]['role'] != 'user':
        raise RequestError(400, 'invalid_messages', 'The last message must have role user.')
    limit = data.get('max_new_tokens', 256)
    if type(limit) is not int or not 1 <= limit <= MAX_OUTPUT:
        raise RequestError(400, 'invalid_max_new_tokens', 'max_new_tokens must be an integer from 1 to 1024.')
    return messages, limit


def reject_constant(value):
    raise ValueError('Non-finite JSON number')


class QwenRuntime:
    def __init__(self, model_dir, max_context):
        # Process-local offline flags only; no downloads or environment installation.
        os.environ['HF_HUB_OFFLINE'] = '1'
        os.environ['TRANSFORMERS_OFFLINE'] = '1'
        import torch
        from transformers import AutoConfig, AutoModelForCausalLM, AutoTokenizer, GenerationConfig
        self.torch = torch
        self.device = torch.device('cuda:0')
        if not torch.cuda.is_available():
            raise RuntimeError('CUDA is unavailable. No CPU or remote fallback is enabled.')
        path = Path(model_dir).expanduser().resolve(strict=True)
        if not path.is_dir():
            raise RuntimeError('model-dir must be a local directory.')
        config = AutoConfig.from_pretrained(str(path), local_files_only=True, trust_remote_code=False)
        if config.model_type != 'qwen3':
            raise RuntimeError('Expected Qwen3 configuration; model files will not be modified.')
        model_limit = getattr(config, 'max_position_embeddings', None)
        if type(model_limit) is not int or not 1 <= max_context <= model_limit:
            raise RuntimeError('max-context must not exceed model max_position_embeddings.')
        self.max_context = max_context
        self.tokenizer = AutoTokenizer.from_pretrained(str(path), local_files_only=True, trust_remote_code=False)
        if not self.tokenizer.chat_template:
            raise RuntimeError('The local tokenizer has no chat_template. Restore official tokenizer files.')
        self.model = AutoModelForCausalLM.from_pretrained(
            str(path), config=config, local_files_only=True, trust_remote_code=False,
            use_safetensors=True, torch_dtype=torch.float16, attn_implementation='sdpa',
            low_cpu_mem_usage=True,
        ).to(self.device).eval()
        # Preserve all official stop IDs, including end-of-text as well as im_end.
        eos = self.model.generation_config.eos_token_id
        if eos is None:
            eos = self.tokenizer.eos_token_id
        if eos is None:
            raise RuntimeError('No EOS token is configured.')
        pad = self.tokenizer.pad_token_id
        if pad is None:
            pad = self.model.generation_config.pad_token_id
        if pad is None:
            pad = eos[0] if isinstance(eos, list) else eos
        self.generation_config = GenerationConfig(
            do_sample=False, num_beams=1, use_cache=True,
            eos_token_id=eos, pad_token_id=pad,
            bos_token_id=self.tokenizer.bos_token_id,
        )

    def health(self):
        return {'status': 'ok', 'model': 'Qwen3-4B-Instruct-2507', 'device': 'cuda:0',
                'torch_dtype': 'float16', 'attention': 'sdpa',
                'max_context': self.max_context, 'max_new_tokens_default': 256,
                'max_new_tokens_limit': MAX_OUTPUT, 'concurrency': 1, 'offline': True}

    def plan(self, messages, max_new_tokens):
        try:
            return self._plan_impl(messages, max_new_tokens)
        except self.torch.cuda.OutOfMemoryError:
            pass
        # Outside the except block: release traceback-held generation tensors first.
        gc.collect()
        self.torch.cuda.empty_cache()
        raise RequestError(503, 'cuda_out_of_memory',
                           'CUDA memory exhausted. No answer was generated. Reduce input/output length or free GPU memory.') from None

    def _plan_impl(self, messages, max_new_tokens):
        torch = self.torch
        inputs = output = None
        try:
            inputs = self.tokenizer.apply_chat_template(
                messages, tokenize=True, add_generation_prompt=True,
                return_tensors='pt', return_dict=True,
            )
            input_tokens = inputs['input_ids'].shape[-1]
            if input_tokens + max_new_tokens > self.max_context:
                raise RequestError(400, 'context_limit',
                                   f'Input has {input_tokens} tokens; input + requested output must be <= {self.max_context}. No input was truncated.')
            inputs = {key: value.to(self.device) for key, value in inputs.items()}
            torch.cuda.synchronize(self.device)
            start = time.perf_counter()
            with torch.inference_mode():
                output = self.model.generate(**inputs, generation_config=self.generation_config,
                                             max_new_tokens=max_new_tokens)
            torch.cuda.synchronize(self.device)
            seconds = time.perf_counter() - start
            generated = output[0, input_tokens:]
            token_ids = generated.tolist()
            result = self.tokenizer.decode(token_ids, skip_special_tokens=True)
            eos = self.generation_config.eos_token_id
            eos_ids = eos if isinstance(eos, list) else [eos]
            return {'text': result, 'input_tokens': input_tokens, 'output_tokens': len(token_ids),
                    'finish_reason': 'stop' if token_ids and token_ids[-1] in eos_ids else 'length',
                    'generation_seconds': round(seconds, 3)}
        finally:
            inputs = output = None


class StrategyServer(HTTPServer):
    request_queue_size = 5
    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(15)
        return connection, address


def handler_for(runtime):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'QwenStrategy/1.0'
        def log_message(self, format, *args):
            pass  # No prompts, URLs, request headers or model text in access logs.

        def reply(self, status, data):
            body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Connection', 'close')
            self.end_headers()
            self.close_connection = True
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            if self.path == '/api/health':
                self.reply(200, runtime.health())
            else:
                self.reply(404, {'error': {'code': 'not_found', 'message': 'Use GET /api/health or POST /api/plan.'}})

        def do_POST(self):
            try:
                if self.path != '/api/plan':
                    raise RequestError(404, 'not_found', 'Use POST /api/plan.')
                if self.headers.get('Transfer-Encoding') is not None:
                    raise RequestError(400, 'invalid_framing', 'Transfer-Encoding is unsupported.')
                lengths = self.headers.get_all('Content-Length', [])
                if len(lengths) != 1 or not lengths[0].isdigit():
                    raise RequestError(411, 'content_length_required', 'One valid Content-Length is required.')
                length = int(lengths[0])
                if not 1 <= length <= MAX_BODY:
                    raise RequestError(413, 'body_too_large', 'Body must be 1 to 131072 bytes.')
                if self.headers.get_content_type() != 'application/json':
                    raise RequestError(415, 'content_type', 'Use Content-Type: application/json.')
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise RequestError(400, 'incomplete_body', 'Incomplete request body.')
                try:
                    data = json.loads(raw.decode('utf-8'), parse_constant=reject_constant)
                except (ValueError, RecursionError):
                    raise RequestError(400, 'invalid_json', 'Body must be valid finite UTF-8 JSON.') from None
                messages, limit = validate_request(data)
                self.reply(200, runtime.plan(messages, limit))
            except RequestError as exc:
                self.reply(exc.status, {'error': {'code': exc.code, 'message': exc.message}})
            except (socket.timeout, TimeoutError):
                self.reply(408, {'error': {'code': 'request_timeout', 'message': 'Request body timed out.'}})
            except Exception as exc:
                print(f'Generation/request failure: {type(exc).__name__}', file=sys.stderr, flush=True)
                self.reply(500, {'error': {'code': 'generation_failed', 'message': 'Generation failed; no fallback answer was produced.'}})
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-dir', required=True)
    parser.add_argument('--host', default='127.0.0.1', choices=['127.0.0.1'])
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--max-context', type=int, default=8192)
    parser.add_argument('--smoke', action='store_true', help='Load, generate one short answer, then exit; no HTTP server.')
    args = parser.parse_args()
    if not 1 <= args.max_context <= 8192:
        parser.error('--max-context must be from 1 to 8192 (deployment safety cap).')
    if not 1 <= args.port <= 65535:
        parser.error('--port must be from 1 to 65535.')
    try:
        runtime = QwenRuntime(args.model_dir, args.max_context)
        if args.smoke:
            print(json.dumps(runtime.plan([{'role': 'user', 'content': '请用一句中文确认你可以生成策略建议。'}], 48), ensure_ascii=False))
            return
        server = StrategyServer((args.host, args.port), handler_for(runtime))
        print(f'Qwen ready: http://{args.host}:{args.port}/api/health ; context={args.max_context}; CUDA 0 FP16', flush=True)
        try:
            server.serve_forever()
        finally:
            server.server_close()
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print(f'Startup failed ({type(exc).__name__}): {exc}', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
