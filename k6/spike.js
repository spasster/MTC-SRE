import http from "k6/http";
import { check } from "k6";

const url = __ENV.BASE_URL || "http://checkout/work";

// 16 rps on top of the 3 rps background. Measured on this cluster:
// two pods at 200m sit near a 1s p95 with throttle above 0.5,
// five pods stay near 0.1s with throttle under 0.2.
// New TCP connection per request, otherwise after scale the open
// connections stay pinned to the pods that existed at the start.
export const options = {
  noConnectionReuse: true,
  noVUConnectionReuse: true,
  scenarios: {
    spike: {
      executor: "constant-arrival-rate",
      rate: Number(__ENV.RATE || 16),
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
