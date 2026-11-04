/**
 * PASSERELLE — envoie les alertes prix Idealo et Keepa de Gmail vers Discord.
 *
 * Tourne chez Google (Apps Script), même PC éteint.
 * 1. Colle ce fichier dans un projet sur script.google.com
 * 2. Lance une fois la fonction "installer" et accepte l'accès à Gmail
 * Ensuite, "verifier" tourne seule toutes les 10 minutes.
 */

// ── Réglages ────────────────────────────────────────────────────────────
const WEBHOOK_ECRANS = "__WEBHOOK_ECRANS__";
const WEBHOOK_TV = "__WEBHOOK_TV__";
const MODELES_ECRANS = __MODELES_ECRANS__;
const MODELES_TV = __MODELES_TV__;
const RECHERCHE = "(from:idealo OR from:keepa) newer_than:7d -label:veilleur-traite";
const LIBELLE = "veilleur-traite";

// ── Lancement ───────────────────────────────────────────────────────────

/** À lancer une seule fois : programme "verifier" toutes les 10 minutes. */
function installer() {
  ScriptApp.getProjectTriggers().forEach(function (t) { ScriptApp.deleteTrigger(t); });
  ScriptApp.newTrigger("verifier").timeBased().everyMinutes(10).create();
  verifier();
}

/** Lit les nouveaux e-mails Idealo/Keepa et poste chaque alerte sur Discord. */
function verifier() {
  const libelle = GmailApp.getUserLabelByName(LIBELLE) || GmailApp.createLabel(LIBELLE);
  const records = PropertiesService.getScriptProperties();

  GmailApp.search(RECHERCHE, 0, 30).forEach(function (fil) {
    try {
      fil.getMessages().forEach(function (mail) { traiter(mail, records); });
      fil.addLabel(libelle);  // traité : on ne le relira plus
    } catch (erreur) {
      // Pas de libellé : on réessaiera au prochain passage.
      console.error("E-mail non traité : " + fil.getFirstMessageSubject() + " (" + erreur + ")");
    }
  });
}

// ── Lecture d'un e-mail ─────────────────────────────────────────────────

/** Les e-mails qui confirment la création d'une alerte ne sont pas des baisses de prix. */
function estConfirmation(sujet) {
  return /activ[ée]e|confirm|cr[ée]{1,2}e avec succ[eè]s|enregistr[ée]e|bienvenue|welcome|activated/i.test(sujet);
}

/** À lancer une fois si des records ont été faussés : repart de zéro. */
function effacerRecords() {
  PropertiesService.getScriptProperties().deleteAllProperties();
  console.log("Records effacés.");
}

function traiter(mail, records) {
  const sujet = mail.getSubject();
  if (estConfirmation(sujet)) return;  // libellé quand même posé : il ne sera pas relu
  const corps = mail.getPlainBody();
  const source = /keepa/i.test(mail.getFrom()) ? "Keepa" : "Idealo";
  const texte = sujet + "\n" + corps;

  const prix = lirePrix(sujet) || lirePrix(corps);
  // Les écrans passent d'abord : un modèle TV court comme "G5" se retrouve
  // aussi dans "Odyssey G5", qui est un écran.
  const estEcran = /[ée]cran|moniteur|monitor|odyssey/i.test(texte);
  const modeleEcran = trouverModele(texte, MODELES_ECRANS);
  const modeleTV = estEcran ? null : trouverModele(texte, MODELES_TV);
  const modele = modeleEcran || modeleTV;
  const estTV = !modeleEcran && (modeleTV !== null || /\b(tv|t[ée]l[ée]viseur)\b/i.test(texte));

  // Record par modèle : on signale si le prix bat le meilleur vu.
  let record = null;
  if (modele && prix !== null) {
    const cle = "record_" + modele;
    const ancien = records.getProperty(cle);
    if (ancien !== null && prix < Number(ancien)) record = Number(ancien);
    if (ancien === null || prix < Number(ancien)) records.setProperty(cle, String(prix));
  }

  const champs = [
    { name: "Prix", value: prix !== null ? euros(prix) : "non trouvé dans l'e-mail", inline: true },
    { name: "Source", value: source, inline: true },
  ];
  if (modele) champs.push({ name: "Modèle", value: modele, inline: true });

  const embed = {
    author: { name: (estTV ? "TV" : "Écran") + " · alerte " + source },
    title: sujet.slice(0, 256),
    description: record !== null
      ? "🏆 Nouveau plus bas (avant : " + euros(record) + ")"
      : "🔔 Alerte prix " + source,
    color: record !== null ? 0x2ecc71 : 0x9b59b6,
    fields: champs,
    footer: { text: "Passerelle Gmail · vérifie le prix avant d'acheter" },
    timestamp: mail.getDate().toISOString(),
  };
  const lien = trouverLien(corps);
  if (lien) embed.url = lien;

  UrlFetchApp.fetch(estTV ? WEBHOOK_TV : WEBHOOK_ECRANS, {
    method: "post",
    contentType: "application/json",
    payload: JSON.stringify({ embeds: [embed] }),
  });
  Utilities.sleep(1000);  // évite de se faire bloquer par Discord
}

/** "1 090,23 €" ou "€ 549.99" → 1090.23 / 549.99 */
function lirePrix(texte) {
  const trouve = texte.match(/(\d{1,3}(?:[ .  ]\d{3})*(?:[.,]\d{1,2})?)\s*€|€\s*(\d{1,3}(?:[ .,  ]\d{3})*(?:[.,]\d{1,2})?)/);
  if (!trouve) return null;
  let nombre = (trouve[1] || trouve[2]).replace(/[\s  ]/g, "");
  if (/[.,]\d{1,2}$/.test(nombre)) {
    nombre = nombre.slice(0, -3).replace(/[.,]/g, "") + "." + nombre.slice(-2).replace(/[.,]/, "");
  } else {
    nombre = nombre.replace(/[.,]/g, "");
  }
  const valeur = parseFloat(nombre);
  return isNaN(valeur) ? null : valeur;
}

/** Même règle que le bot : "C5" matche "OLED55C5". */
function trouverModele(texte, modeles) {
  const bas = texte.toLowerCase();
  for (let i = 0; i < modeles.length; i++) {
    const motif = modeles[i].toLowerCase().replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    if (new RegExp("(?<![a-z])" + motif + "(?!\\d)").test(bas)) return modeles[i];
  }
  return null;
}

function trouverLien(corps) {
  const liens = corps.match(/https?:\/\/[^\s<>")\]]+/g) || [];
  return liens.find(function (l) { return /idealo|keepa|amazon/i.test(l); }) || null;
}

function euros(prix) {
  return prix.toFixed(2).replace(".", ",").replace(/\B(?=(\d{3})+(?!\d))/g, " ") + " €";
}
