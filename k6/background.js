import http from "k6/http";
import { check } from "k6";

const url = __ENV.BASE_URL || "http://checkout/work";

export const options = {
  scenarios: {
    background: {
      executor: "constant-arrival-rate",
      rate: Number(__ENV.RATE || 3),
      timeUnit: "1s",
      duration: __ENV.DURATION || "24h",
      preAllocatedVUs: 5,
      maxVUs: 20,
    },
  },
  discardResponseBodies: true,
};

export default function () {
  const response = http.get(url, { timeout: "5s" });
  check(response, { "status 200": (item) => item.status === 200 });
}
