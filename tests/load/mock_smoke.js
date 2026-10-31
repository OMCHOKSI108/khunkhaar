// Local load smoke test for the mock API. Bounded by config performance.k6 limits:
// max_vus 20, max_duration_seconds 120. Never point BASE_URL at a remote host.
import http from 'k6/http';
import { check, sleep } from 'k6';

const BASE = __ENV.BASE_URL || 'http://127.0.0.1:8731';

export const options = {
  vus: 5,
  duration: '10s',
  thresholds: {
    http_req_failed: ['rate<0.01'],
    http_req_duration: ['p(95)<500'],
  },
};

export default function () {
  const health = http.get(`${BASE}/health`);
  check(health, {
    'health is 200': (r) => r.status === 200,
    'health names the mock service': (r) => r.json('service') === 'khunkhaar-mock',
  });

  const items = http.get(`${BASE}/items?limit=3`);
  check(items, {
    'items is 200': (r) => r.status === 200,
    'items returns the requested count': (r) => r.json('items').length === 3,
  });

  sleep(0.2);
}
