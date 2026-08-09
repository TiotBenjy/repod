/**
 * computeIndexStaleness : bandeau de fraîcheur de l'index des sources externes.
 *
 * Bug corrigé : le bandeau retient la source la PLUS ancienne, et une source
 * désactivée dans les paramètres est exclue de tous les jobs de sync côté
 * backend, elle n'obtient donc jamais de last_sync. Résultat : une seule
 * source désactivée (ici debian-bookworm-non-free) suffisait à afficher
 * "Index des sources externes non synchronisé jamais synchronisé" en
 * permanence, alors que les 119 autres venaient d'être synchronisées. Aucune
 * synchro ne pouvait faire disparaître l'avertissement.
 */
import { describe, it, expect } from "vitest";
import { computeIndexStaleness } from "../components/PackageList";

const hoursAgo = (h) => new Date(Date.now() - h * 3_600_000).toISOString();

describe("computeIndexStaleness", () => {
  it("ignore les sources désactivées jamais synchronisées", () => {
    const { indexStale } = computeIndexStaleness([
      { source_id: "ubuntu-noble", last_sync: hoursAgo(1), enabled: true },
      { source_id: "debian-bookworm", last_sync: hoursAgo(2), enabled: true },
      { source_id: "debian-bookworm-non-free", last_sync: null, enabled: false },
    ]);
    expect(indexStale).toBe(false);
  });

  it("signale un vrai retard sur une source activée", () => {
    const { indexStale, oldestSyncHours } = computeIndexStaleness([
      { source_id: "ubuntu-noble", last_sync: hoursAgo(1), enabled: true },
      { source_id: "debian-bookworm", last_sync: hoursAgo(72), enabled: true },
    ]);
    expect(indexStale).toBe(true);
    expect(oldestSyncHours).toBeGreaterThan(24);
  });

  it("signale une source activée jamais synchronisée", () => {
    const { indexStale, oldestSyncHours } = computeIndexStaleness([
      { source_id: "ubuntu-noble", last_sync: hoursAgo(1), enabled: true },
      { source_id: "debian-bookworm", last_sync: null, enabled: true },
    ]);
    expect(indexStale).toBe(true);
    expect(Number.isFinite(oldestSyncHours)).toBe(false);
  });

  it("compte les sources sans champ enabled (backend antérieur au correctif)", () => {
    const { indexStale } = computeIndexStaleness([
      { source_id: "ubuntu-noble", last_sync: hoursAgo(1) },
      { source_id: "debian-bookworm", last_sync: hoursAgo(200) },
    ]);
    expect(indexStale).toBe(true);
  });

  it("ne signale rien quand toutes les sources sont désactivées", () => {
    const { indexStale } = computeIndexStaleness([
      { source_id: "debian-bookworm-non-free", last_sync: null, enabled: false },
    ]);
    expect(indexStale).toBe(false);
  });

  it("ne signale rien sur une liste vide (statut indisponible)", () => {
    expect(computeIndexStaleness([]).indexStale).toBe(false);
    expect(computeIndexStaleness(undefined).indexStale).toBe(false);
  });
});
