// ==UserScript==
// @name         Barchart -> BIMachine Futures Bridge
// @namespace    https://bimachine.com.br/
// @version      1.0.0
// @description  Reads delayed CC/SB futures from the logged-in Barchart browser session and sends them to the BIMachine Render API.
// @match        https://www.barchart.com/*
// @grant        GM_xmlhttpRequest
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_registerMenuCommand
// @connect      futures-cc-sb-peccin.onrender.com
// ==/UserScript==

(function () {
  "use strict";

  const DEFAULT_ENDPOINT =
    "https://futures-cc-sb-peccin.onrender.com/bridge/ingest";
  const ROOTS = ["CC", "SB"];
  const INTERVAL_MS = 5 * 60 * 1000;
  const FIRST_RUN_DELAY_MS = 8000;

  const FIELDS = [
    "symbol",
    "contractSymbol",
    "lastPrice",
    "priceChange",
    "openPrice",
    "highPrice",
    "lowPrice",
    "previousPrice",
    "volume",
    "openInterest",
    "tradeTime",
  ].join(",");

  let running = false;
  let badge;

  function setStatus(text, type = "idle") {
    if (!badge) {
      badge = document.createElement("div");
      badge.style.cssText = [
        "position:fixed",
        "right:12px",
        "bottom:12px",
        "z-index:2147483647",
        "padding:8px 10px",
        "border-radius:6px",
        "font:12px/1.3 Arial,sans-serif",
        "box-shadow:0 2px 8px rgba(0,0,0,.25)",
        "max-width:320px",
        "cursor:pointer",
      ].join(";");
      badge.title = "Click to synchronize now";
      badge.addEventListener("click", () => synchronize(true));
      document.documentElement.appendChild(badge);
    }

    const styles = {
      idle: ["#eef2ff", "#273469"],
      working: ["#fff8e1", "#705500"],
      ok: ["#e8f5e9", "#1b5e20"],
      error: ["#ffebee", "#b71c1c"],
    };
    const [background, color] = styles[type] || styles.idle;
    badge.style.background = background;
    badge.style.color = color;
    badge.textContent = `Barchart Bridge: ${text}`;
  }

  function getCookie(name) {
    const prefix = `${name}=`;
    for (const part of document.cookie.split(";")) {
      const trimmed = part.trim();
      if (trimmed.startsWith(prefix)) {
        return trimmed.slice(prefix.length);
      }
    }
    return null;
  }

  function decodeXsrf(value) {
    let decoded = value;
    for (let index = 0; index < 2; index += 1) {
      try {
        decoded = decodeURIComponent(decoded);
      } catch (_error) {
        break;
      }
    }
    return decoded;
  }

  function toNumber(value) {
    if (value === null || value === undefined || value === "") {
      return null;
    }
    const normalized =
      typeof value === "string" ? value.replace(/,/g, "").trim() : value;
    const number = Number(normalized);
    return Number.isFinite(number) ? number : null;
  }

  function toInteger(value) {
    const number = toNumber(value);
    return number === null ? null : Math.trunc(number);
  }

  async function fetchRoot(root) {
    const parameters = new URLSearchParams({
      fields: FIELDS,
      list: "futures.contractInRoot",
      root,
      raw: "1",
    });

    const headers = {
      Accept: "application/json, text/plain, */*",
      "X-Requested-With": "XMLHttpRequest",
    };

    const cookie = getCookie("XSRF-TOKEN");
    if (cookie) {
      headers["X-XSRF-TOKEN"] = decodeXsrf(cookie);
    }

    const response = await fetch(
      `/proxies/core-api/v1/quotes/get?${parameters.toString()}`,
      {
        method: "GET",
        credentials: "include",
        cache: "no-store",
        headers,
      }
    );

    const text = await response.text();
    let body;
    try {
      body = JSON.parse(text);
    } catch (_error) {
      throw new Error(
        `Barchart returned non-JSON content for ${root} (HTTP ${response.status})`
      );
    }

    if (!response.ok) {
      throw new Error(
        `Barchart request failed for ${root} (HTTP ${response.status})`
      );
    }

    const records = Array.isArray(body.data) ? body.data : [];
    if (records.length === 0) {
      throw new Error(`Barchart returned no contracts for ${root}`);
    }

    return records
      .map((item) => ({
        Root: root,
        Contract: String(item.contractSymbol || item.symbol || "").toUpperCase(),
        Last: toNumber(item.lastPrice),
        Change: toNumber(item.priceChange),
        Open: toNumber(item.openPrice),
        High: toNumber(item.highPrice),
        Low: toNumber(item.lowPrice),
        Previous: toNumber(item.previousPrice),
        Volume: toInteger(item.volume),
        Open_Int: toInteger(item.openInterest),
        Time: item.tradeTime ?? null,
      }))
      .filter((item) => item.Contract.startsWith(root));
  }

  function sendToRender(payload, endpoint, token) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method: "POST",
        url: endpoint,
        headers: {
          "Content-Type": "application/json",
          "X-Bridge-Token": token,
        },
        data: JSON.stringify(payload),
        timeout: 30000,
        onload(response) {
          if (response.status >= 200 && response.status < 300) {
            try {
              resolve(JSON.parse(response.responseText));
            } catch (_error) {
              resolve({ status: "ok" });
            }
            return;
          }
          reject(
            new Error(
              `Render rejected the bridge update (HTTP ${response.status})`
            )
          );
        },
        ontimeout() {
          reject(new Error("Timeout while sending data to Render"));
        },
        onerror() {
          reject(new Error("Network error while sending data to Render"));
        },
      });
    });
  }

  async function synchronize(manual = false) {
    if (running) {
      return;
    }

    const endpoint = await GM_getValue("bridgeEndpoint", DEFAULT_ENDPOINT);
    const token = await GM_getValue("bridgeToken", "");

    if (!token) {
      setStatus("configure the token in the Tampermonkey menu", "error");
      if (manual) {
        alert(
          "Configure BARCHART_BRIDGE_TOKEN using the Tampermonkey menu command."
        );
      }
      return;
    }

    running = true;
    setStatus("synchronizing CC and SB...", "working");

    try {
      const pageText = document.body?.innerText?.toLowerCase() || "";
      if (
        pageText.includes("verify that you're not a robot") ||
        pageText.includes("verify you are human") ||
        pageText.includes("enable javascript")
      ) {
        throw new Error("Complete the Barchart browser verification first");
      }

      const results = [];
      for (const root of ROOTS) {
        const rows = await fetchRoot(root);
        results.push(...rows);
      }

      if (results.length === 0) {
        throw new Error("No Barchart contracts were collected");
      }

      const response = await sendToRender(
        {
          source: "Barchart Web",
          capturedAt: new Date().toISOString(),
          data: results,
        },
        endpoint,
        token
      );

      const currentTime = new Date().toLocaleTimeString();
      setStatus(`${response.rows || results.length} rows sent at ${currentTime}`, "ok");
      console.info("[Barchart Bridge] update completed", response);
    } catch (error) {
      console.error("[Barchart Bridge] synchronization failed", error);
      setStatus(error.message || String(error), "error");
    } finally {
      running = false;
    }
  }

  GM_registerMenuCommand("Configure BIMachine bridge", async () => {
    const currentEndpoint = await GM_getValue("bridgeEndpoint", DEFAULT_ENDPOINT);
    const currentToken = await GM_getValue("bridgeToken", "");

    const endpoint = prompt("Render ingest URL:", currentEndpoint);
    if (!endpoint) {
      return;
    }

    const token = prompt(
      "BARCHART_BRIDGE_TOKEN configured in Render:",
      currentToken
    );
    if (!token) {
      return;
    }

    await GM_setValue("bridgeEndpoint", endpoint.trim());
    await GM_setValue("bridgeToken", token.trim());
    setStatus("configuration saved; click here to test", "idle");
  });

  GM_registerMenuCommand("Synchronize now", () => synchronize(true));

  setStatus("waiting for first synchronization", "idle");
  window.setTimeout(() => synchronize(false), FIRST_RUN_DELAY_MS);
  window.setInterval(() => synchronize(false), INTERVAL_MS);
})();
