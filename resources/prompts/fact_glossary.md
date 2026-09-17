Below are short names taken from notes about one person, each with facts that mention them. For each, give the plain-language thing it refers to, in at most FOUR words - the words someone would use if they did not know the short name.

Give the shortest phrase that identifies it and nothing more. No institution names, no dates, no qualifiers: for a course code answer with the subject alone ("Distributed Computing"), not who teaches it or where it is taught.

Only expand names that are specific to THIS person's life and would be meaningless to anyone else: a course code, a project codename, a lab or team name, an employer's internal system. Those are the ones a person cannot search for without knowing them already.

Return null for everything else. In particular return null for standard industry terminology - programming languages, protocols, file formats, hardware, well-known libraries and frameworks - however abbreviated. Someone asking about those already uses the same words, so spelling them out adds length and helps nobody.

Also return null for a name whose meaning is not stated in its facts, or one you would have to guess at. Null is the right answer whenever you are unsure.

{blocks}

Reply with JSON only: {{"glossary": {{"<name>": "<meaning or null>"}}}}
