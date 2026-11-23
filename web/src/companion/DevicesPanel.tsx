import { useCallback, useEffect, useState } from "react";
import { Laptop, Smartphone } from "lucide-react";

import { copyTextToClipboard } from "@/lib/clipboard";
import { useClient } from "@/providers/ClientProvider";

import { fetchDevices, newPairCode, type PairedDevice } from "./device";
import { dayLabel } from "./presentation";
import { Skeleton } from "./Skeleton";

/** The device this page runs on (set once it has signed in), to mark it and keep it out of 移除. */
let currentDeviceId: string | null = null;

export function setCurrentDevice(id: string | null): void {
  currentDeviceId = id;
}

/** 设置 → 设备: what is connected to this server, a code for one more, and removing one. */
export function DevicesPanel({ onDisconnect }: { onDisconnect: () => void }) {
  const { client, token } = useClient();
  const [devices, setDevices] = useState<PairedDevice[] | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const [code, setCode] = useState<{ code: string; expires_at: number; link: string } | null>(null);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);

  const load = useCallback(() => {
    setLoadFailed(false);
    fetchDevices(token)
      .then((rows) => {
        setDevices(rows);
        setLoadFailed(false);
      })
      .catch(() => {
        setLoadFailed(true);
      });
  }, [token]);
  useEffect(load, [load]);

  const addDevice = () => {
    setError(null);
    newPairCode(token)
      .then(setCode)
      .catch(() => setError("没拿到配对码，稍后再试"));
  };
  const remove = (id: string) => {
    client
      .requestMutation<{ devices: PairedDevice[] }>("companion.devices.remove", { id })
      .then((body) => setDevices(body.devices))
      .catch(() => setError("没能移除，稍后再试"));
  };

  return (
    <div className="flex flex-col gap-6">
      <section className="flex flex-col gap-1.5">
        <h2 className="c-muted px-3.5 text-[13px]">已连接的设备</h2>
        <ul className="c-card flex flex-col overflow-hidden rounded-2xl">
          {loadFailed ? (
            <li className="flex items-center justify-between gap-3 px-4 py-3 text-[14px]">
              <span className="c-danger">设备列表没读出来，检查一下网络</span>
              <button
                type="button"
                onClick={load}
                className="c-pressable min-h-11 rounded-full px-4 text-[13.5px] font-medium"
                style={{ background: "var(--c-fill)" }}
              >
                重试
              </button>
            </li>
          ) : devices === null ? (
            <li role="status" aria-busy="true" aria-label="正在读取设备" className="divide-y c-divider">
              {[0, 1].map((index) => (
                <div key={index} className="flex items-center gap-3 px-4 py-3">
                  <Skeleton className="h-5 w-5 shrink-0 rounded-md" />
                  <div className="min-w-0 flex-1 space-y-1.5">
                    <Skeleton className={index === 0 ? "h-3.5 w-32" : "h-3.5 w-24"} />
                    <Skeleton className="h-2.5 w-40" />
                  </div>
                </div>
              ))}
            </li>
          ) : devices.length === 0 ? (
            <li className="c-muted px-4 py-3 text-[14px]">这台是服务器本机，不需要配对。</li>
          ) : (
            devices.map((device) => {
              const mine = device.id === currentDeviceId;
              const Icon = /电脑|Mac|浏览器/.test(device.name) ? Laptop : Smartphone;
              return (
                <li key={device.id} className="c-divider flex items-center gap-3 border-b px-4 py-3 last:border-b-0">
                  <Icon className="c-muted h-5 w-5 shrink-0" aria-hidden="true" />
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-[15px]">
                      {device.name}
                      {mine && <span className="c-muted text-[13px]"> · 这台</span>}
                    </p>
                    <p className="c-muted text-[12.5px]">
                      连接于 {dayLabel(device.paired_at)} · 最近 {dayLabel(device.last_seen)}
                    </p>
                  </div>
                  {!mine && (
                    <button type="button" onClick={() => remove(device.id)} className="c-danger c-pressable min-h-11 rounded-full px-3 text-[14px]">
                      移除
                    </button>
                  )}
                </li>
              );
            })
          )}
        </ul>
        <p className="c-muted px-3.5 text-[12.5px] leading-relaxed">
          每台设备只需配对一次，之后打开就能用。移除后，那台设备要重新配对才能再连上。
        </p>
      </section>

      <section className="flex flex-col gap-1.5">
        <h2 className="c-muted px-3.5 text-[13px]">添加新设备</h2>
        <div className="c-card flex flex-col items-center gap-2 rounded-2xl px-4 py-4 text-center">
          {code ? (
            <>
              <p className="font-mono text-[28px] font-semibold tracking-[0.2em]">{code.code}</p>
              <button
                type="button"
                onClick={() => void copyTextToClipboard(code.link).then((ok) => ok && setCopied(true))}
                className="c-pressable min-h-10 rounded-full px-4 text-[14px] font-medium"
                style={{ background: "var(--c-fill)" }}
              >
                {copied ? "已复制连接链接" : "复制连接链接"}
              </button>
              <p className="c-muted text-[13px] leading-relaxed">
                在新设备上打开这个链接就会自动连上；也可以在伙伴里填服务器 {window.location.host} 和配对码。
                <br />
                {new Date(code.expires_at).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })} 前有效，只能用一次。
              </p>
              <button type="button" onClick={load} className="c-pressable mt-1 min-h-10 rounded-full px-4 text-[14px]" style={{ background: "var(--c-fill)" }}>
                配好了，刷新列表
              </button>
            </>
          ) : (
            <button type="button" onClick={addDevice} className="c-primary min-h-11 w-full rounded-full text-[15px] font-semibold">
              生成配对码
            </button>
          )}
          {error && (
            <p className="c-danger text-[13px]" role="alert">
              {error}
            </p>
          )}
        </div>
      </section>

      {currentDeviceId && (
        <section className="flex flex-col gap-1.5">
          {confirming ? (
            <div className="c-card flex flex-col gap-2 rounded-2xl p-4">
              <p className="text-[14px] leading-relaxed">断开后，这台设备要重新输入配对码才能再连上。服务器上的任务和记录不受影响。</p>
              <div className="flex gap-2">
                <button type="button" onClick={onDisconnect} className="min-h-11 flex-1 rounded-full text-[15px] font-semibold text-white" style={{ background: "var(--c-danger-fill)" }}>
                  断开
                </button>
                <button type="button" onClick={() => setConfirming(false)} className="c-pressable min-h-11 flex-1 rounded-full text-[15px]" style={{ background: "var(--c-fill)" }}>
                  取消
                </button>
              </div>
            </div>
          ) : (
            <button type="button" onClick={() => setConfirming(true)} className="c-card c-danger c-pressable min-h-12 rounded-2xl text-[15px]">
              断开这台设备
            </button>
          )}
        </section>
      )}
    </div>
  );
}
