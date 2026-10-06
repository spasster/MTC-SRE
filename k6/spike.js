import http from "k6/http";
import { check } from "k6";

const url = __ENV.BASE_URL || "http://checkout/work";

// Дополнительные 3 запроса в секунду поверх фоновых 3 запросов.
// Результат зависит от CPU и mesh; измерения хранятся в docs/results.
// Новое соединение даёт балансировщику выбрать и добавленные реплики.
export const options = {
  noConnectionReuse: true,
  noVUConnectionReuse: true,
  scenarios: {
    spike: {
      executor: "constant-arrival-rate",
      rate: Number(__ENV.RATE || 3),
      timeUnit: "1s",
      duration: __ENV.DURATION || "6m",
      preAllocatedVUs: 40,
      maxVUs: 150,
    },
  },
  discardResponseBodies: true,
};

export default function () {
  const response = http.get(url, {
    timeout: "10s",
    noConnectionReuse: true,
    headers: { Connection: "close" },
  });
  check(response, { "status 200": (item) => item.status === 200 });
}

// Итог со стороны клиента дополняет метрики завершённых серверных запросов.
export function handleSummary(data) {
  const summary = {};
  for (const name of ["http_reqs", "http_req_duration", "http_req_failed", "dropped_iterations", "checks"]) {
    summary[name] = data.metrics[name]?.values || {};
  }
  return { stdout: "MTC_SUMMARY " + JSON.stringify(summary) + "\n" };
}
