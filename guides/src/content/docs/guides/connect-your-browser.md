---
title: Connect your browser
description: Give north a browser profile for web tasks. Use your own Chrome profile with your sign-ins, or a separate profile that north manages.
---

This guide connects a browser profile to north, so north can do web tasks.
You can use a profile that you already sign in with, or a separate profile that north manages.

## What a browser profile is

A browser profile is one set of sign-ins, cookies, tabs and extensions in a browser.
Give each profile a purpose, for example "University work and job applications".
north reads the purposes and picks one profile for each task.
If the choice is not clear, north asks you through your approval mode.

There are two types of profile:

| Type | Badge in the list | What north uses |
| --- | --- | --- |
| Existing profile | **Existing browser** | A profile that you already use in Chrome, Chromium or Brave. north can reach its signed-in sessions, cookies, tabs and extensions. north never copies your credentials. |
| North-managed profile | **North-managed** | A separate browser that north starts. It is separate from your personal profiles and cannot copy them. |

Use an existing profile when a task needs a site where you are already signed in.
Use a North-managed profile to keep north apart from your personal browsing.

## Before you start

- Start north and open the dashboard with `north web`. See the [Quickstart](/north/guides/quickstart/).
- For an existing profile, you need Node 20 or later and npm.
- The extension needs Chrome 116 or later.

## Open the Browser profiles panel

The **Browser profiles** panel is in two places:

- In Setup, at step 3, **Browser profiles**.
- In **Settings**, under **Connections**.

## Add an existing profile

1. Click **Find existing profiles**.
   north reads only the names and folders of your profiles. It never reads passwords, cookies or history.
2. Click the profile that you want. Each button shows the browser and the profile name.
3. In **Use this profile for**, write the work that north does with this profile.
4. Check the **Name**. Change **Browser data directory** only if north picked the wrong browser.
5. Select **Allow North to select this profile.** A profile that you find this way starts with this box clear.
6. Click **Save profile**.

## Connect the extension

An existing profile needs north's copy of the TaskWindow extension.
You load it into the profile one time.

1. In the profile row, click **Connect**. north shows the steps and a folder path.
2. Open Chrome with that profile and go to `chrome://extensions`.
3. Turn on **Developer mode**, at the top right.
4. If TaskWindow from the Chrome Web Store is installed in this profile, remove it.
5. Click **Load unpacked** and choose the folder that north shows. To copy the path, click **Copy folder**.
6. Wait. The extension pairs with north by itself within 10 minutes.

Load the folder in this profile only.
If the folder is loaded in a different profile too, north does not use it.

If the profile does not pair, click **Connect** again.
north then writes a new one-time pairing code into the same folder.

## Test the connection

1. Click **Test connection**.
2. The row shows **Checking · may be waiting in the Inbox**.
3. If a card waits, click **Open Inbox →** in the panel and answer the card.
4. When the test passes, the row shows **Profile verified · login not checked**.

The test is a normal browser call, so it follows your approval mode like other calls.
The test proves that north reaches the correct profile.
It does not check that you are signed in to a site.

If the profile is not open, north waits for the extension to connect.
On macOS, north then opens the profile in the background.

**Test connection** is off while the profile is switched off.
When you save a change to a profile, north clears the old test results. Run the test again.

## Add a North-managed profile

1. Click **Add North-managed profile**.
2. Type a **Name**. In **Use this profile for**, write the work that north does with this profile.
3. To see the browser and help north when necessary, keep **Show the browser window so I can help when needed** selected.
4. Keep **Allow North to select this profile.** selected.
5. Click **Save profile**.

A North-managed profile does not need the extension or the **Connect** step.

## How north works in your Chrome

- north works in its own background window. It does not open tasks in your windows.
- Each task gets its own tab group in that window.
- north does not touch your tabs and does not bring Chrome forward.
- If Chrome comes forward while north opens its window, north gives the focus back to your app one time.
- When a task ends, north saves a screenshot of each tab that the task left open. Then it closes the tab.
- A tab stays open while a card from that task still waits for you.

## How north updates the extension

north's copy of the extension does not come from the Chrome Web Store.
Chrome never updates it, so it never updates by itself.

north updates the copy. The first time north checks a profile after north starts, it compares the copy with the version that north ships.
If the files are different, north writes the new files over the copy in the same folder.
Then north tells the extension to reload.
The pairing stays, so you do not need to click **Connect** again.
If the reload request fails, the old copy continues to work.

## Switch off a profile

Click **Switch off**. north stops using the profile.
Your browser and your sign-ins do not change.
To use the profile again, click **Switch on**.
To change a profile, click **Edit**.

## Troubleshooting

| Message | What to do |
| --- | --- |
| No supported browser profiles found. Chrome, Chromium and Brave are supported. | Install one of these browsers, or click **Add North-managed profile**. |
| Existing browser profiles need Node 20+ and npm. | Install Node 20 or later with npm. Then click **Connect** again. |
| Could not install the pinned TaskWindow daemon. Check npm/network and retry. | Make sure that npm works and that you have a network connection. Then click **Connect** again. |
| ... browser bridge did not start. See ... daemon.log. | Read the `daemon.log` file that the message names. |
| ... is not connected. Open Browser profiles in North's dashboard and connect it. | Do the steps in "Connect the extension" again. |
| ... is paired with a different extension. Connect it again. | Click **Connect** and load north's folder again. |
| North's folder for ... is loaded in ..., not only in .... | Remove the extension from the other profiles. Keep it in this profile only. Then click **Connect**. |
| ... browser extension did not come back after an update. Retry. | Click **Test connection** again. |
