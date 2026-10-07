import http from "k6/http";
import { check } from "k6";

export const options = {
  noConnectionReuse: true,
  scenarios: {
    limited: {
      executor: "constant-arrival-rate",
      rate: 2,
      timeUnit: "1s",
      duration: "180s",
      preAllocatedVUs: 10,
      maxVUs: 30,
    },
  },
};

export default function () {
  const response = http.get("http://checkout/work", { timeout: "5s" });
  check(response, {
    "Полный checkout успешен": (item) => {
      try {
        const body = item.json();
        return item.status === 200 && body.status === "ok" && body.payment === "ok" && body.cart_seq > 0;
      } catch (_) {
        return false;
      }
    },
  });
}

export function handleSummary(data) {
  const summary = {};
  for (const name of ["http_reqs", "http_req_duration", "http_req_failed", "dropped_iterations", "checks"]) {
    summary[name] = data.metrics[name]?.values || {};
  }
  return { stdout: "MTC_SUMMARY " + JSON.stringify(summary) + "\n" };
}
