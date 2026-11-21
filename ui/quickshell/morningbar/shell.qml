import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import QtQuick

ShellRoot {
    id: shell

    CompositorAdapter {
        id: compositor
    }

    PanelWindow {
        id: window
        objectName: "morningbarWindow"
        property bool expanded: false
        property bool closing: false
        property bool manualRequested: false
        property bool readyPulsePlayed: false
        property int selectedStoryIndex: 0
        property int selectedCategoryIndex: 0
        property string lastRequestId: ""
        readonly property string previewState: Quickshell.env("MORNINGBAR_PREVIEW_STATE") || ""
        readonly property bool preview: previewState.length > 0
        readonly property string stateRoot: Quickshell.env("XDG_STATE_HOME")
            || ((Quickshell.env("HOME") || "") + "/.local/state")
        readonly property string morningStateDir: stateRoot + "/morningbar"
        readonly property string localDate: Quickshell.env("MORNINGBAR_LOCAL_DATE")
            || Qt.formatDate(new Date(), "yyyy-MM-dd")
        readonly property string statusPath: Quickshell.env("MORNINGBAR_STATUS_FILE")
            || (morningStateDir + "/status.json")
        readonly property string briefingPath: Quickshell.env("MORNINGBAR_BRIEFING_FILE")
            || (morningStateDir + "/briefing-" + localDate + ".json")
        readonly property string uiStatePath: Quickshell.env("MORNINGBAR_UI_STATE_FILE")
            || (morningStateDir + "/ui-state.json")
        readonly property string uiRequestPath: Quickshell.env("MORNINGBAR_UI_REQUEST_FILE")
            || (morningStateDir + "/ui-request.json")
        readonly property string cliPath: Quickshell.env("MORNINGBAR_CLI") || "morningbar"
        readonly property string accent: "#bacbff"
        readonly property color panelColor: "#f91a222c"
        readonly property color mutedColor: "#a2afbf"
        readonly property color quietColor: "#798695"
        readonly property var statusData: parseJson(statusFile.text())
        readonly property var briefingData: parseJson(briefingFile.text())
        readonly property var uiStateData: parseJson(uiStateFile.text())
        readonly property bool ready: previewState === "ready" || previewState === "expanded"
            || (statusData.stage === "ready" && briefingData.date === localDate)
        readonly property bool gathering: previewState === "gathering"
            || (!preview && !ready && (statusData.stage === "trigger-starting"
                || statusData.stage === "starting" || statusData.stage === "waiting-network"
                || statusData.stage === "collecting" || statusData.stage === "collecting-cache"
                || statusData.stage === "collected" || statusData.stage === "ranking"
                || statusData.stage === "writing"))
        readonly property bool dismissedPreview: previewState === "dismissed"
        readonly property bool dismissedToday: !preview
            && uiStateData.dismissed_local_day === localDate
        readonly property var stories: briefingData && Array.isArray(briefingData.stories)
            ? briefingData.stories : []
        readonly property var categoryOptions: buildCategoryOptions()
        readonly property string activeCategory: categoryOptions.length > 0
            ? categoryOptions[Math.min(selectedCategoryIndex, categoryOptions.length - 1)] : "All"
        readonly property var visibleStories: filterStories()
        readonly property var activeStory: visibleStories.length > 0
            ? visibleStories[Math.min(selectedStoryIndex, visibleStories.length - 1)] : null
        readonly property bool safeToShow: preview || (compositor.unlocked && !compositor.fullscreen)
        readonly property bool activeForToday: gathering || ready || manualRequested
        readonly property bool shouldShow: preview || (safeToShow && activeForToday && !dismissedToday)

        anchors {
            top: true
        }
        margins.top: 14
        exclusiveZone: 0
        aboveWindows: true
        color: "transparent"
        implicitWidth: expanded ? 900 : 430
        implicitHeight: expanded ? 950 : 74
        Behavior on implicitWidth {
            NumberAnimation { duration: compositor.reducedMotion ? 0 : 300; easing.type: Easing.OutCubic }
        }
        Behavior on implicitHeight {
            NumberAnimation { duration: compositor.reducedMotion ? 0 : 300; easing.type: Easing.OutCubic }
        }
        visible: shouldShow
        focusable: expanded
        WlrLayershell.namespace: "morningbar"
        WlrLayershell.keyboardFocus: expanded ? WlrKeyboardFocus.OnDemand : WlrKeyboardFocus.None
        mask: Region {
            item: card
            radius: window.expanded ? 26 : 38
        }

        function parseJson(textValue) {
            if (!textValue || textValue.length === 0)
                return ({})
            try {
                return JSON.parse(textValue)
            } catch (error) {
                return ({})
            }
        }

        function buildCategoryOptions() {
            var values = ["All"];
            for (var i = 0; i < stories.length; ++i) {
                var name = stories[i].category || "Other";
                if (values.indexOf(name) === -1)
                    values.push(name);
            }
            return values;
        }

        function filterStories() {
            if (activeCategory === "All")
                return stories;
            return stories.filter(function(item) { return item.category === activeCategory; });
        }

        function setCategory(index) {
            if (categoryOptions.length === 0)
                return;
            selectedCategoryIndex = (index + categoryOptions.length) % categoryOptions.length;
            selectedStoryIndex = 0;
            if (storyList)
                storyList.contentY = 0;
        }

        function cycleCategory(step) {
            setCategory(selectedCategoryIndex + step);
        }

        function moveStory(step) {
            if (visibleStories.length === 0)
                return;
            selectedStoryIndex = (selectedStoryIndex + step + visibleStories.length) % visibleStories.length;
            var target = storyRepeater.itemAt(selectedStoryIndex);
            if (!target)
                return;
            if (target.y < storyList.contentY)
                storyList.contentY = target.y;
            else if (target.y + target.height > storyList.contentY + storyList.height)
                storyList.contentY = target.y + target.height - storyList.height;
        }

        function toggleExpanded() {
            expanded = !expanded;
            if (expanded)
                selectedStoryIndex = 0;
        }

        function timeAgo(value) {
            var stamp = Date.parse(value || "");
            if (isNaN(stamp))
                return "recent";
            var minutes = Math.max(0, Math.floor((Date.now() - stamp) / 60000));
            if (minutes < 60)
                return minutes + "m ago";
            var hours = Math.floor(minutes / 60);
            if (hours < 24)
                return hours + "h ago";
            return Math.floor(hours / 24) + "d ago";
        }

        function activeStatus() {
            if (previewState === "gathering")
                return "Gathering your morning";
            if (previewState === "dismissed")
                return "Dismissed for today";
            if (ready)
                return "Good morning, Noalane · " + stories.length + " stories";
            if (gathering)
                return "Gathering your morning";
            if (statusData.stage === "failed")
                return "Briefing could not finish";
            return "Your morning briefing";
        }

        function sourceProgress() {
            if (previewState === "gathering")
                return "34 of 65 sources · building quietly";
            var checked = Number(statusData.progress || 0);
            var total = Number(statusData.total || statusData.sources_checked || 0);
            if (total > 0)
                return checked + " of " + total + " sources · " + (statusData.stage || "starting");
            return gathering ? "Preparing your selected sources" : "No collection is running · press r to start";
        }

        function consumeRequest() {
            if (preview || !requestFile.loaded)
                return;
            var request = parseJson(requestFile.text());
            if (request.action !== "show" || request.date !== localDate
                    || !request.request_id || request.request_id === lastRequestId)
                return;
            lastRequestId = request.request_id;
            manualRequested = true;
            expanded = request.expand === true;
            selectedStoryIndex = 0;
            Qt.callLater(function() { requestFile.setText(""); });
        }

        function runCommand(args) {
            if (preview)
                return;
            var command = [cliPath];
            for (var i = 0; i < args.length; ++i)
                command.push(args[i]);
            Quickshell.execDetached(command);
        }

        function openStory(story) {
            if (story && story.link)
                Qt.openUrlExternally(story.link);
        }

        function feedbackStory(story, vote) {
            if (story)
                runCommand(["feedback", story.id, "--vote", vote]);
        }

        function saveStory(story) {
            if (story)
                runCommand(["save", story.id]);
        }

        function dismissForToday() {
            if (preview)
                return;
            closing = true;
            expanded = false;
            runCommand(["dismiss"]);
            closeDelay.start();
        }

        function rerun() {
            if (preview || gathering)
                return;
            expanded = false;
            readyPulsePlayed = false;
            runCommand(["run"]);
        }

        function updateReadyPulse() {
            if (ready && !readyPulsePlayed) {
                readyPulsePlayed = true;
                if (!compositor.reducedMotion)
                    readyPulse.start();
            }
        }

        FileView {
            id: statusFile
            path: window.statusPath
            watchChanges: true
            printErrors: false
            onFileChanged: reload()
            onTextChanged: window.updateReadyPulse()
        }

        FileView {
            id: briefingFile
            path: window.briefingPath
            watchChanges: true
            printErrors: false
            onFileChanged: reload()
            onTextChanged: window.updateReadyPulse()
        }

        FileView {
            id: uiStateFile
            path: window.uiStatePath
            watchChanges: true
            printErrors: false
            onFileChanged: reload()
        }

        FileView {
            id: requestFile
            path: window.uiRequestPath
            watchChanges: true
            printErrors: false
            onFileChanged: reload()
            onTextChanged: window.consumeRequest()
        }

        Timer {
            id: closeDelay
            interval: compositor.reducedMotion ? 0 : 300
            onTriggered: Qt.quit()
        }

        SequentialAnimation {
            id: readyPulse
            NumberAnimation { target: card; property: "scale"; to: 1.018; duration: 180; easing.type: Easing.OutCubic }
            NumberAnimation { target: card; property: "scale"; to: 1.0; duration: 280; easing.type: Easing.OutCubic }
        }

        Rectangle {
            id: card
            objectName: "morningbarCard"
            anchors.fill: parent
            radius: window.expanded ? 26 : 37
            color: window.panelColor
            border.width: 1
            border.color: "#38ffffff"
            clip: true

            Behavior on radius {
                NumberAnimation { duration: compositor.reducedMotion ? 0 : 280; easing.type: Easing.OutCubic }
            }
            Behavior on opacity {
                NumberAnimation { duration: compositor.reducedMotion ? 0 : 280; easing.type: Easing.OutCubic }
            }
            Behavior on scale {
                NumberAnimation { duration: compositor.reducedMotion ? 0 : 220; easing.type: Easing.OutCubic }
            }
            opacity: window.closing ? 0.24 : (window.dismissedPreview ? 0.5 : 1.0)

            Item {
                id: compactContent
                anchors.fill: parent
                visible: !window.expanded

                Row {
                    anchors.fill: parent
                    anchors.leftMargin: 19
                    anchors.rightMargin: 19
                    spacing: 13

                    Item {
                        width: 32
                        height: 32
                        anchors.verticalCenter: parent.verticalCenter

                        Rectangle {
                            width: 28
                            height: 28
                            anchors.centerIn: parent
                            radius: 14
                            color: "transparent"
                            border.width: 2
                            border.color: "#3f4b59"
                            visible: window.gathering
                        }
                        Rectangle {
                            width: 5
                            height: 5
                            x: 13.5
                            y: 0
                            radius: 3
                            color: window.accent
                            visible: window.gathering

                            RotationAnimation on rotation {
                                from: 0
                                to: 360
                                duration: 1250
                                loops: Animation.Infinite
                                running: window.gathering && !compositor.reducedMotion
                            }
                        }
                        Rectangle {
                            width: 9
                            height: 9
                            anchors.centerIn: parent
                            radius: 5
                            color: window.accent
                            visible: !window.gathering
                        }
                    }

                    Column {
                        width: 282
                        anchors.verticalCenter: parent.verticalCenter
                        spacing: 3

                        Text {
                            objectName: "pillTitle"
                            width: parent.width
                            text: window.activeStatus()
                            color: "#f2f5fa"
                            font.family: "Noto Sans"
                            font.pixelSize: 15
                            font.weight: Font.DemiBold
                            elide: Text.ElideRight
                        }
                        Text {
                            width: parent.width
                            text: window.dismissedPreview ? "It will stay quiet until you summon it"
                                : (window.ready ? (window.briefingData.mode === "basic" ? "Basic mode · tap to read" : "Curated for you · tap to read")
                                    : window.sourceProgress())
                            color: window.mutedColor
                            font.family: "Noto Sans"
                            font.pixelSize: 11
                            elide: Text.ElideRight
                        }
                    }

                    Item { width: 1; height: 1 }

                    Text {
                        text: window.ready && window.briefingData.mode === "basic" ? "BASIC" : "OPEN"
                        color: window.ready && window.briefingData.mode === "basic" ? "#d5bd87" : window.accent
                        font.family: "Noto Sans"
                        font.pixelSize: 9
                        font.weight: Font.Bold
                        anchors.verticalCenter: parent.verticalCenter
                        horizontalAlignment: Text.AlignRight
                    }
                }

                MouseArea {
                    anchors.fill: parent
                    acceptedButtons: Qt.LeftButton
                    onClicked: window.toggleExpanded()
                }
            }

            Column {
                id: expandedContent
                anchors.fill: parent
                anchors.margins: 22
                spacing: 11
                visible: window.expanded

                Row {
                    width: parent.width
                    height: 40
                    spacing: 12

                    Column {
                        width: parent.width - 160
                        spacing: 2
                        Text {
                            text: "MORNING BAR"
                            color: window.accent
                            font.family: "Noto Sans"
                            font.pixelSize: 10
                            font.weight: Font.Bold
                            font.letterSpacing: 1.5
                        }
                        Text {
                            text: window.ready ? window.activeStatus() : window.activeStatus()
                            width: parent.width
                            color: "#f4f6fa"
                            font.family: "Noto Sans"
                            font.pixelSize: 16
                            font.weight: Font.DemiBold
                            elide: Text.ElideRight
                        }
                    }

                    Text {
                        width: 86
                        visible: window.ready && (window.briefingData.mode === "basic"
                            || Number(window.briefingData.unavailable_sources || 0) > 0)
                        text: (window.briefingData.mode === "basic" ? "BASIC" : "")
                            + (Number(window.briefingData.unavailable_sources || 0) > 0
                                ? (window.briefingData.mode === "basic" ? " · " : "")
                                    + Number(window.briefingData.unavailable_sources) + " OFFLINE" : "")
                        color: window.briefingData.mode === "basic" ? "#e8cb8f" : window.quietColor
                        font.family: "Noto Sans"
                        font.pixelSize: 9
                        font.weight: Font.Bold
                        elide: Text.ElideRight
                        anchors.verticalCenter: parent.verticalCenter
                    }
                    Rectangle {
                        width: 36
                        height: 36
                        radius: 18
                        color: "#222c37"
                        anchors.verticalCenter: parent.verticalCenter
                        Text {
                            anchors.centerIn: parent
                            text: "×"
                            color: "#c4ccd6"
                            font.pixelSize: 21
                        }
                        MouseArea {
                            anchors.fill: parent
                            onClicked: window.expanded = false
                        }
                    }
                }

                Rectangle {
                    id: hero
                    width: parent.width
                    height: 150
                    radius: 18
                    color: "#24303b"
                    border.width: 1
                    border.color: "#2c3946"

                    Column {
                        anchors.fill: parent
                        anchors.margins: 17
                        spacing: 8

                        Row {
                            width: parent.width
                            spacing: 9
                            Rectangle {
                                width: heroCategory.implicitWidth + 18
                                height: 22
                                radius: 11
                                color: "#3a4858"
                                Text {
                                    id: heroCategory
                                    anchors.centerIn: parent
                                    text: window.activeStory ? window.activeStory.category : "TODAY"
                                    color: "#d6e0ed"
                                    font.family: "Noto Sans"
                                    font.pixelSize: 10
                                    font.weight: Font.DemiBold
                                }
                            }
                            Text {
                                visible: window.activeStory && window.activeStory.manual_priority
                                text: "ARCH ACTION · ALWAYS SHOWN"
                                color: "#f1cd88"
                                font.family: "Noto Sans"
                                font.pixelSize: 9
                                font.weight: Font.Bold
                                anchors.verticalCenter: parent.verticalCenter
                            }
                            Item { width: 1; height: 1 }
                            Text {
                                text: window.gathering ? "COLLECTING"
                                    : (window.ready ? (window.activeStory ? "TOP STORY" : "QUIET MORNING") : "NOT RUN YET")
                                color: window.quietColor
                                font.family: "Noto Sans"
                                font.pixelSize: 9
                                font.weight: Font.Bold
                                anchors.verticalCenter: parent.verticalCenter
                                horizontalAlignment: Text.AlignRight
                            }
                        }

                        Text {
                            width: parent.width
                            text: window.activeStory ? window.activeStory.title
                                : (window.ready ? "A quiet morning across your interests."
                                    : (window.gathering ? "The briefing is taking shape." : "No briefing for today yet."))
                            color: "#f1f4f8"
                            font.family: "Noto Sans"
                            font.pixelSize: 19
                            font.weight: Font.DemiBold
                            wrapMode: Text.WordWrap
                            maximumLineCount: 2
                            elide: Text.ElideRight
                        }

                        Text {
                            width: parent.width
                            text: window.activeStory ? window.activeStory.summary
                                : (window.ready ? "No new stories matched your interests in the recent feed window."
                                    : (window.gathering ? window.sourceProgress() : "Press r to start a fresh collection."))
                            color: window.mutedColor
                            font.family: "Noto Sans"
                            font.pixelSize: 12
                            wrapMode: Text.WordWrap
                            maximumLineCount: 2
                            elide: Text.ElideRight
                        }
                    }
                    MouseArea {
                        anchors.fill: parent
                        z: -1
                        onClicked: window.openStory(window.activeStory)
                    }
                }

                Column {
                    width: parent.width
                    spacing: 7
                    Text {
                        text: "THE SHORT VERSION"
                        color: window.quietColor
                        font.family: "Noto Sans"
                        font.pixelSize: 9
                        font.weight: Font.Bold
                        font.letterSpacing: 1.2
                    }
                    Repeater {
                        model: window.ready ? (window.briefingData.tldr || [])
                            : [window.gathering ? window.sourceProgress() : "Press r to start a fresh collection."]
                        delegate: Row {
                            required property string modelData
                            width: expandedContent.width
                            height: 20
                            spacing: 9
                            Rectangle {
                                width: 4
                                height: 4
                                radius: 2
                                color: window.accent
                                anchors.verticalCenter: parent.verticalCenter
                            }
                            Text {
                                width: parent.width - 16
                                text: modelData
                                color: "#d1d8e1"
                                font.family: "Noto Sans"
                                font.pixelSize: 11
                                elide: Text.ElideRight
                                anchors.verticalCenter: parent.verticalCenter
                            }
                        }
                    }
                }

                Flickable {
                    id: categoryScroll
                    width: parent.width
                    height: 31
                    contentWidth: categoryRow.implicitWidth
                    contentHeight: height
                    clip: true
                    flickableDirection: Flickable.HorizontalFlick

                    Row {
                        id: categoryRow
                        spacing: 7
                        Repeater {
                            model: window.categoryOptions
                            delegate: Rectangle {
                                required property string modelData
                                required property int index
                                width: chipLabel.implicitWidth + 22
                                height: 27
                                radius: 14
                                color: index === window.selectedCategoryIndex ? "#3b4d63" : "#232d38"
                                border.width: index === window.selectedCategoryIndex ? 1 : 0
                                border.color: "#667c99"
                                Text {
                                    id: chipLabel
                                    anchors.centerIn: parent
                                    text: modelData
                                    color: index === window.selectedCategoryIndex ? "#eaf0fb" : "#a9b5c3"
                                    font.family: "Noto Sans"
                                    font.pixelSize: 10
                                }
                                MouseArea {
                                    anchors.fill: parent
                                    onClicked: {
                                        window.setCategory(index);
                                        categoryScroll.contentX = Math.max(0, Math.min(categoryScroll.contentWidth - categoryScroll.width, x + parent.x - categoryScroll.width / 3));
                                    }
                                }
                            }
                        }
                    }
                }

                Rectangle {
                    width: parent.width
                    height: 1
                    color: "#2c3743"
                }

                Row {
                    width: parent.width
                    height: 24
                    Text {
                        text: window.activeCategory === "All" ? "STORIES" : window.activeCategory.toUpperCase()
                        color: "#b9c2ce"
                        font.family: "Noto Sans"
                        font.pixelSize: 10
                        font.weight: Font.Bold
                        font.letterSpacing: 1
                    }
                    Item { width: 12; height: 1 }
                    Text {
                        text: window.visibleStories.length + " updates · 36 hour window"
                        color: window.quietColor
                        font.family: "Noto Sans"
                        font.pixelSize: 10
                        anchors.verticalCenter: parent.verticalCenter
                    }
                }

                Flickable {
                    id: storyList
                    objectName: "storyList"
                    width: parent.width
                    height: Math.max(110, parent.height - y - 37)
                    contentWidth: width
                    contentHeight: storyColumn.implicitHeight
                    clip: true
                    boundsBehavior: Flickable.StopAtBounds

                    Column {
                        id: storyColumn
                        width: storyList.width
                        spacing: 8

                        Repeater {
                            id: storyRepeater
                            model: window.visibleStories
                            delegate: Rectangle {
                                id: storyCard
                                required property var modelData
                                required property int index
                                readonly property var story: modelData
                                width: storyColumn.width
                                height: storyContent.implicitHeight + 22
                                radius: 15
                                color: index === window.selectedStoryIndex ? "#273441" : "#202a34"
                                border.width: index === window.selectedStoryIndex ? 1 : 0
                                border.color: "#42546a"

                                Column {
                                    id: storyContent
                                    anchors.fill: parent
                                    anchors.margins: 11
                                    spacing: 5

                                    Row {
                                        width: parent.width
                                        height: 16
                                        spacing: 8
                                        Text {
                                            text: modelData.category || "Other"
                                            color: window.accent
                                            font.family: "Noto Sans"
                                            font.pixelSize: 9
                                            font.weight: Font.DemiBold
                                            anchors.verticalCenter: parent.verticalCenter
                                        }
                                        Text {
                                            width: Math.max(40, parent.width - 230)
                                            text: (modelData.source_names || []).join(" · ") + "  ·  " + window.timeAgo(modelData.published_at)
                                            color: window.quietColor
                                            font.family: "Noto Sans"
                                            font.pixelSize: 9
                                            elide: Text.ElideRight
                                            anchors.verticalCenter: parent.verticalCenter
                                        }
                                        Row {
                                            spacing: 3
                                            anchors.verticalCenter: parent.verticalCenter
                                            Repeater {
                                                model: 5
                                                delegate: Rectangle {
                                                    required property int index
                                                    width: 5
                                                    height: 5
                                                    radius: 3
                                                    color: index < Math.ceil(Number(storyCard.story.importance || 1) / 2) ? window.accent : "#495461"
                                                }
                                            }
                                        }
                                    }

                                    Text {
                                        width: parent.width
                                        text: modelData.title || "Untitled story"
                                        color: "#edf1f6"
                                        font.family: "Noto Sans"
                                        font.pixelSize: 13
                                        font.weight: Font.DemiBold
                                        wrapMode: Text.WordWrap
                                        maximumLineCount: 2
                                        elide: Text.ElideRight
                                    }

                                    Text {
                                        width: parent.width
                                        text: modelData.summary || "Open the source for details."
                                        color: "#b3becb"
                                        font.family: "Noto Sans"
                                        font.pixelSize: 10
                                        wrapMode: Text.WordWrap
                                        maximumLineCount: 2
                                        elide: Text.ElideRight
                                    }

                                    Row {
                                        width: parent.width
                                        spacing: 6
                                        Text {
                                            width: parent.width - 290
                                            text: "Why it matters · " + (modelData.why_it_matters || "Matches your selected interests.")
                                            color: "#8795a5"
                                            font.family: "Noto Sans"
                                            font.pixelSize: 9
                                            elide: Text.ElideRight
                                            anchors.verticalCenter: parent.verticalCenter
                                        }
                                        Item { width: 4; height: 1 }
                                        Repeater {
                                            model: ["Open", "Like", "Less", "Save"]
                                            delegate: Text {
                                                required property string modelData
                                                required property int index
                                                text: modelData
                                                color: index === 3 ? window.accent : "#aeb8c4"
                                                font.family: "Noto Sans"
                                                font.pixelSize: 9
                                                anchors.verticalCenter: parent.verticalCenter
                                                MouseArea {
                                                    anchors.fill: parent
                                                    anchors.margins: -4
                                                    onClicked: {
                                                        if (index === 0) window.openStory(story_);
                                                        else if (index === 1) window.feedbackStory(story_, "up");
                                                        else if (index === 2) window.feedbackStory(story_, "down");
                                                        else window.saveStory(story_);
                                                    }
                                                }
                                                property var story_: storyCard.story
                                            }
                                        }
                                    }
                                }

                                MouseArea {
                                    anchors.fill: parent
                                    z: -1
                                    onClicked: window.selectedStoryIndex = index
                                }
                            }
                        }

                        Text {
                            visible: window.visibleStories.length === 0
                            width: storyColumn.width
                            text: window.ready ? "Nothing new matched this category. A quiet day is useful too."
                                : (window.gathering ? "Sources are being checked. This list will fill as the briefing is prepared."
                                    : "No briefing is available yet. Start a collection with r.")
                            color: window.mutedColor
                            font.family: "Noto Sans"
                            font.pixelSize: 12
                            wrapMode: Text.WordWrap
                            padding: 12
                        }
                    }
                }

                Row {
                    width: parent.width
                    height: 21
                    Text {
                        width: parent.width - 95
                        text: window.dismissedPreview ? "Dismissing · it will stay quiet today"
                            : "j/k or ↑/↓ move   ·   Enter open   ·   Tab categories   ·   Esc collapse"
                        color: window.quietColor
                        font.family: "Noto Sans"
                        font.pixelSize: 9
                        elide: Text.ElideRight
                        anchors.verticalCenter: parent.verticalCenter
                    }
                    Item { width: 1; height: 1 }
                    Text {
                        text: "d dismiss   ·   r rerun"
                        color: window.quietColor
                        font.family: "Noto Sans"
                        font.pixelSize: 9
                        anchors.verticalCenter: parent.verticalCenter
                    }
                }
            }
        }

        Item {
            id: keySink
            objectName: "morningbarKeySink"
            anchors.fill: parent
            visible: true
            focus: window.expanded
            Keys.enabled: window.expanded
            Keys.onPressed: function(event) {
                if (event.key === Qt.Key_Escape) {
                    window.expanded = false;
                    event.accepted = true;
                } else if (event.key === Qt.Key_Down || event.key === Qt.Key_J) {
                    window.moveStory(1);
                    event.accepted = true;
                } else if (event.key === Qt.Key_Up || event.key === Qt.Key_K) {
                    window.moveStory(-1);
                    event.accepted = true;
                } else if (event.key === Qt.Key_Right) {
                    window.cycleCategory(1);
                    event.accepted = true;
                } else if (event.key === Qt.Key_Left) {
                    window.cycleCategory(-1);
                    event.accepted = true;
                } else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
                    window.openStory(window.activeStory);
                    event.accepted = true;
                } else if (event.key === Qt.Key_Tab) {
                    window.cycleCategory(event.modifiers & Qt.ShiftModifier ? -1 : 1);
                    event.accepted = true;
                } else if (event.key === Qt.Key_D) {
                    window.dismissForToday();
                    event.accepted = true;
                } else if (event.key === Qt.Key_R) {
                    window.rerun();
                    event.accepted = true;
                }
            }
        }

        Component.onCompleted: {
            if (previewState === "expanded")
                expanded = true;
            if (previewState === "dismissed")
                manualRequested = true;
            consumeRequest();
            updateReadyPulse();
        }
    }
}
