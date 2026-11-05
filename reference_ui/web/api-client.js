/* Published API client: session cookies, idempotency and observable acceptance. No DOM code. */
(function (root) {
  'use strict';
  class RobotClient {
    constructor(base = '') { this.base = base; this.pendingKeys = new Map(); this.operationKeys = new Map(); }
    async request(method, path, body, key) {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), path === '/api/v1/wifi/scan' ? 35000 : method === 'GET' ? 4000 : 20000);
      const headers = {};
      const identityBody = path === '/api/v1/wifi/connect' && body ? {...body, password: '[credential]'} : body;
      const identity = path + ':' + JSON.stringify(identityBody);
      const isAction = method === 'POST' && ['/api/v1/missions', '/api/v1/speech',
        '/api/v1/system/shutdown', '/api/v1/system/reboot',
        '/api/v1/wifi/connect', '/api/v1/missions/go-to', '/api/v1/missions/go-to-pose', '/api/v1/public/start-tour'].includes(path);
      if (body !== undefined) headers['Content-Type'] = 'application/json';
      if (isAction) {
        key = key || this.pendingKeys.get(identity) || crypto.randomUUID();
        this.pendingKeys.set(identity, key);
        headers['Idempotency-Key'] = key;
      }
      try {
        const response = await fetch(this.base + path, {method, headers, credentials: 'include',
          signal: controller.signal, body: body === undefined ? undefined : JSON.stringify(body)});
        const data = await response.json();
        if (isAction && data.id) {
          this.operationKeys.set(data.id, identity);
        } else if (isAction && response.status < 500) {
          this.pendingKeys.delete(identity);
        }
        if (data.error && typeof data.error === 'object') {
          return {ok: false, status: response.status, data: {...data,
            reason: data.error.message, message: data.error.message, error: data.error.code}};
        }
        return {ok: response.ok, status: response.status, data};
      } catch (_) {
        return {ok: false, status: 0, data: {message: isAction ? 'outcome_unknown' : 'transport_unavailable'}};
      } finally { clearTimeout(timer); }
    }
    async awaitAcceptance(operation, timeoutMs = 15000) {
      const deadline = Date.now() + timeoutMs;
      while (operation.status === 'pending' && Date.now() < deadline) {
        await new Promise(resolve => setTimeout(resolve, 150));
        const response = await this.request('GET', '/api/v1/state');
        if (!response.ok || response.data.instance_id !== operation.instance_id) break;
        operation = (response.data.operations || []).find(item => item.id === operation.id) || operation;
      }
      if (!['pending', 'unknown'].includes(operation.status)) {
        const identity = this.operationKeys.get(operation.id);
        if (identity) this.pendingKeys.delete(identity);
        this.operationKeys.delete(operation.id);
      }
      if (['running', 'succeeded'].includes(operation.status)) {
        return {ok: true, status: 202, data: operation};
      }
      const unknown = ['pending', 'unknown'].includes(operation.status);
      const result = operation.result || {};
      const message = unknown ? 'outcome_unknown' : result.message || result.detail || 'operation_' + operation.status;
      return {ok: false, status: unknown ? 504 : 409, data: {message, reason: message, operation}};
    }
    events() {
      const base = this.base || location.origin;
      return new WebSocket(base.replace(/^http/, 'ws') + '/api/v1/events');
    }
  }
  root.RobotClient = RobotClient;
})(typeof window === 'undefined' ? globalThis : window);
